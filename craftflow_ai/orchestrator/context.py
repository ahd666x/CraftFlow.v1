"""
سازندهٔ Context.

وظیفه: تبدیل وضعیت سامانه به یک قطعهٔ متن/JSON کوچک و مرتب که به LLM داده
می‌شود. اصل حاکم: **LLM هرگز کل دیتابیس را نمی‌بیند.** فقط:

  * تاریخ امروز و اینکه روز کاری است یا نه
  * کاربر جاری و دسترسی‌های مؤثرش
  * فهرست ابزارهایی که واقعاً اجازهٔ اجرا دارد
  * خلاصهٔ سبک تولید/انبار (کمّی، نه ردیف‌به‌ردیف)
  * نتیجهٔ ابزارهای فراخوانی‌شده

مقادیر مشتق‌شده (مثل «ایستگاه شلوغ») به‌عنوان راهنمای اولیه داده می‌شوند تا
مدل مجبور نباشد برای یک سؤال ساده کل ابزار را صدا بزند، ولی این‌ها
جایگزین داده نیستند و مدل باید برای اعداد دقیق ابزار اجرا کند.
"""
import logging

from django.utils import timezone

from craftflow_ai import config
from craftflow_ai.permissions.policy import permissions_for_user
from craftflow_ai.services import queries

logger = logging.getLogger('craftflow_ai.orchestrator.context')

# سقف حجم هر نتیجهٔ ابزار که به مدل داده می‌شود (جلوگیری از انفجار context)
MAX_TOOL_RESULT_CHARS = 12000


class ContextBuilder:
    """ساخت Context کم‌حجم برای هر نوبت گفتگو."""

    def __init__(self, user, registry=None):
        self.user = user
        self._registry = registry

    @property
    def registry(self):
        if self._registry is None:
            from craftflow_ai.tools import get_registry
            self._registry = get_registry()
        return self._registry

    def base_snapshot(self):
        """خلاصهٔ کمی از وضعیت کارخانه — فقط اعداد تجمیعی."""
        snapshot = {}
        try:
            status = queries.production_status()
            snapshot['production'] = {
                'today': status['today'],
                'orders_total': status['orders']['total'],
                'orders_active': status['orders']['active'],
                'orders_completed': status['orders']['completed'],
                'tasks_pending': status['tasks']['pending'],
                'tasks_waiting': status['tasks']['waiting'],
                'tasks_completed': status['tasks']['completed'],
                'busiest_stage': (
                    status['busiest_stage']['stage_label'] if status['busiest_stage'] else None
                ),
                'open_defects': status['quality']['open_defects'],
            }
        except Exception:
            logger.exception('Context snapshot: production status failed')
            snapshot['production'] = {'available': False}

        try:
            inventory = queries.inventory_status(limit=1)
            snapshot['inventory'] = {
                'total_materials': inventory['total_materials'],
                'low_stock_count': inventory['low_stock_count'],
                'out_of_stock_count': inventory['out_of_stock_count'],
                'open_material_issues': inventory['open_material_issues']['total'],
            }
        except Exception:
            logger.exception('Context snapshot: inventory status failed')
            snapshot['inventory'] = {'available': False}

        return snapshot

    def build(self, user_message, tool_results=None, conversation_history=None):
        """خروجی نهایی: دیکشنری بخش‌های context."""
        return {
            'current_date': self._date_block(),
            'current_user': self._user_block(),
            'permissions': permissions_for_user(self.user),
            'available_tools': self._tool_block(),
            'factory_snapshot': self.base_snapshot(),
            'tool_results': self._format_results(tool_results or []),
            'conversation_history': self._format_history(conversation_history or []),
            'limits': {
                'max_tool_calls': config.get_max_tool_calls(),
                'read_only': True,
                'note': (
                    'در فاز ۱ فقط عملیات خواندنی مجاز است؛ هیچ ابزار نوشتنی وجود ندارد.'
                ),
            },
            'user_message': (user_message or '')[:4000],
        }

    def _date_block(self):
        from product.models import Holiday

        today = queries.jalali_today()
        block = {
            'today_jalali': today.strftime('%Y/%m/%d'),
            'today_gregorian': today.togregorian().isoformat(),
            'now': timezone.now().isoformat(),
        }
        try:
            from product.utils import is_working_day
            block['is_working_day'] = is_working_day(today)
        except Exception:
            logger.exception('Context: working-day check failed')
            block['is_working_day'] = None

        try:
            upcoming = Holiday.objects.filter(date__gte=today.togregorian()).order_by('date')[:5]
            block['upcoming_holidays'] = [h.date.isoformat() for h in upcoming]
        except Exception:
            logger.exception('Context: holidays failed')
        return block

    def _user_block(self):
        user = self.user
        if not user or not getattr(user, 'is_authenticated', False):
            return {'authenticated': False}
        groups = list(user.groups.values_list('name', flat=True)) if user.pk else []
        return {
            'authenticated': True,
            'username': user.get_username(),
            'display_name': (user.get_full_name() or user.get_username()),
            'is_superuser': user.is_superuser,
            'groups': groups,
        }

    def _tool_block(self):
        tools = self.registry.available_for(self.user)
        return [{
            'name': tool.name,
            'description': tool.description,
            'permission': tool.permission,
            'input_schema': tool.input_schema,
        } for tool in tools]

    def _format_results(self, tool_results):
        formatted = []
        for entry in tool_results:
            tool_name = entry.get('tool_name', '')
            result = entry.get('result')
            tool = None
            try:
                tool = self.registry.get(tool_name)
            except Exception:
                tool = None

            block = {
                'tool': tool_name,
                'label': tool.to_activity_label() if tool else tool_name,
                'arguments': entry.get('arguments') or {},
            }
            if isinstance(result, dict) and 'success' in result:
                payload = result.get('data') if result.get('success') else result.get('error')
                text = _to_text(payload)
                if len(text) > MAX_TOOL_RESULT_CHARS:
                    text = text[:MAX_TOOL_RESULT_CHARS] + '\n… (بخشی از نتیجه حذف شد)'
                    block['truncated'] = True
                block['success'] = bool(result.get('success'))
                block['result'] = text
            else:
                block['success'] = False
                block['result'] = _to_text(result)
            formatted.append(block)
        return formatted

    def _format_history(self, history):
        max_history = config.get_max_history()
        recent = list(history)[-max_history:] if max_history else []
        return [{
            'role': item.get('role'),
            'content': (item.get('content') or '')[:1500],
        } for item in recent if item.get('content')]


def _to_text(value, indent=0):
    """تبدیل ساختار به متن فشرده و خوانا برای مدل."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    pad = '  ' * indent
    if isinstance(value, list):
        if not value:
            return '[]'
        items = value[:60]
        lines = [f'{pad}- {_to_text(item, indent + 1).strip()}' for item in items]
        if len(value) > len(items):
            lines.append(f'{pad}… و {len(value) - len(items)} مورد دیگر')
        return '\n'.join(lines)
    if isinstance(value, dict):
        if not value:
            return '{}'
        lines = []
        for key, item in value.items():
            rendered = _to_text(item, indent + 1)
            if isinstance(item, (dict, list)) and rendered.strip():
                lines.append(f'{pad}{key}:')
                lines.append(rendered)
            else:
                lines.append(f'{pad}{key}: {rendered}')
        return '\n'.join(lines)
    return str(value)