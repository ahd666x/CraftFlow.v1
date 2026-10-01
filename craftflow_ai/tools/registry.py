"""
Tool Registry — قلب لایهٔ AI.

این ماژول تنها جایی است که LLM می‌تواند به سیستم دسترسی پیدا کند. سه
محدودیت سخت اینجا اعمال می‌شود و هیچ‌جای دیگری قابل دور زدن نیست:

1. **Whitelist** — فقط Toolهایی که صریحاً ``register`` شده‌اند وجود دارند.
   هیچ Tool عمومی (``execute_sql`` / ``run_python`` / ...) وجود ندارد و
   ساختنشان ممکن نیست، چون نام‌ها از همین رجیستری می‌آیند.
2. **Read-only در فاز ۱** — هر Tool باید ``read_only=True`` باشد. رجیستری
   در صورت ثبت Tool نوشتنی، آن را رد می‌کند.
3. **دسترسی** — هر Tool یک ``permission`` دارد که پیش از اجرا بررسی می‌شود.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from craftflow_ai.permissions.errors import (
    ToolDisabledError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolValidationError,
)

logger = logging.getLogger('craftflow_ai.tools.registry')

# فاز ۱ فقط-خواندنی است. تغییر این مقدار یک تصمیم آگاهانه در فاز ۲ است.
READ_ONLY_PHASE = True


# ----------------------------------------------------------------------
# قرارداد پاسخ استاندارد
# ----------------------------------------------------------------------

def ok(data: Any, **metadata) -> Dict[str, Any]:
    from django.utils import timezone

    payload = {'success': True, 'data': data}
    meta = {'generated_at': timezone.now().isoformat(), 'source': 'craftflow'}
    meta.update(metadata)
    payload['metadata'] = meta
    return payload


def fail(code: str, message: str, **detail) -> Dict[str, Any]:
    error = {'code': code, 'message': message}
    if detail:
        error['detail'] = detail
    return {'success': False, 'error': error}


@dataclass
class Tool:
    """تعریف یک Tool فقط-خواندنی."""
    name: str
    description: str
    permission: str
    handler: Callable[..., Any]
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    read_only: bool = True
    category: str = 'general'

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError('Tool name must be a non-empty string')
        if not callable(self.handler):
            raise ValueError(f'Tool {self.name} handler must be callable')
        if not self.input_schema:
            self.input_schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}

    # -- نمایش عمومی به LLM ------------------------------------------
    def to_schema(self) -> Dict[str, Any]:
        """شمای استاندارد function-calling. هیچ اطلاعات داخلی لو نمی‌رود."""
        return {
            'name': self.name,
            'description': self.description,
            'input_schema': self.input_schema,
        }

    def to_public_metadata(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'description': self.description,
            'permission': self.permission,
            'read_only': self.read_only,
            'category': self.category,
            'input_schema': self.input_schema,
            'output_schema': self.output_schema,
        }

    def to_activity_label(self) -> str:
        """برچسب فارسی کوتاه برای نمایش «AI در حال بررسی…» در UI."""
        return ACTIVITY_LABELS.get(self.name, self.name)

    # -- اعتبارسنجی و اجرا -------------------------------------------
    def validate_arguments(self, arguments: Any) -> Dict[str, Any]:
        if arguments is None:
            return {}
        if not isinstance(arguments, dict):
            raise ToolValidationError(
                f'آرگومان‌های ابزار {self.name} باید شیء باشد.',
                code='INVALID_ARGUMENTS',
                detail={'received_type': type(arguments).__name__},
            )
        schema = self.input_schema or {}
        properties = schema.get('properties', {}) or {}
        required = schema.get('required', []) or []

        for key in required:
            if key not in arguments or arguments[key] in (None, ''):
                raise ToolValidationError(
                    f'آرگومان «{key}» برای {self.name} الزامی است.',
                    code='MISSING_ARGUMENT',
                    detail={'argument': key, 'tool': self.name},
                )

        cleaned = {}
        for key, value in arguments.items():
            spec = properties.get(key)
            if spec is None:
                if schema.get('additionalProperties') is False:
                    raise ToolValidationError(
                        f'آرگومان ناشناخته «{key}» برای {self.name}.',
                        code='UNKNOWN_ARGUMENT',
                        detail={'argument': key, 'allowed': sorted(properties)},
                    )
                cleaned[key] = value
                continue
            cleaned[key] = _coerce(value, spec, self.name, key)
        return cleaned

    def run(self, **kwargs) -> Dict[str, Any]:
        args = self.validate_arguments(kwargs)
        try:
            result = self.handler(**args)
        except Exception as exc:
            logger.exception('Tool %s failed', self.name)
            raise ToolExecutionError(
                f'اجرای ابزار {self.name} ناموفق بود.',
                detail={'tool': self.name, 'exception': type(exc).__name__},
            ) from exc
        if not isinstance(result, dict) or 'success' not in result:
            raise ToolExecutionError(
                f'خروجی ابزار {self.name} با ساختار مورد انتظار مطابقت ندارد.',
                detail={'tool': self.name},
            )
        return result


ACTIVITY_LABELS = {
    'get_open_orders': 'سفارش‌های باز',
    'get_order_details': 'جزئیات سفارش',
    'find_delayed_orders': 'سفارش‌های عقب‌افتاده',
    'get_production_status': 'وضعیت تولید',
    'get_pending_tasks': 'تسک‌های در انتظار',
    'find_production_bottlenecks': 'گلوگاه‌های تولید',
    'get_inventory_status': 'موجودی انبار',
    'get_material_requirements': 'نیاز مواد سفارش',
    'find_material_shortages': 'کمبود مواد',
    'generate_production_report': 'گزارش تولید',
    'get_quality_status': 'وضعیت خرابی‌ها',
    # فاز ۲
    'analyze_order_health': 'تحلیل سلامت سفارش',
    'analyze_order_delay': 'تحلیل تأخیر سفارش',
    'analyze_station_bottleneck': 'تحلیل گلوگاه ایستگاه',
    'analyze_material_impact': 'تحلیل تأثیر مواد',
    'simulate_material_availability': 'شبیه‌سازی موجودی مواد',
    'simulate_order_priority': 'شبیه‌سازی اولویت سفارش',
}


def _coerce(value, spec, tool_name, key):
    expected = spec.get('type')
    try:
        if expected == 'integer':
            if isinstance(value, bool):
                raise ValueError('bool')
            return int(value)
        if expected == 'number':
            return float(value)
        if expected == 'string':
            return str(value)
        if expected == 'boolean':
            if isinstance(value, bool):
                return value
            if str(value).strip().lower() in ('true', '1', 'yes'):
                return True
            if str(value).strip().lower() in ('false', '0', 'no'):
                return False
            raise ValueError('not a boolean')
        if expected == 'array':
            if isinstance(value, list):
                return value
            raise ValueError('not an array')
    except (TypeError, ValueError) as exc:
        raise ToolValidationError(
            f'مقدار آرگومان «{key}» در {tool_name} از نوع {expected} نیست.',
            code='INVALID_ARGUMENT_TYPE',
            detail={'argument': key, 'expected': expected, 'tool': tool_name},
        ) from exc
    return value


class ToolRegistry:
    """رجیستری ابزارهای قابل فراخوانی توسط LLM."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not isinstance(tool, Tool):
            raise TypeError('register() expects a Tool instance')
        if tool.name in self._tools:
            raise ValueError(f'Tool "{tool.name}" is already registered')
        if READ_ONLY_PHASE and not tool.read_only:
            raise ToolDisabledError(
                f'ابزار نوشتنی «{tool.name}» در فاز ۱ مجاز نیست.',
                detail={'tool': tool.name},
            )
        self._tools[tool.name] = tool
        logger.debug('Registered AI tool: %s', tool.name)
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(
                f'ابزار «{name}» در رجیستری وجود ندارد.',
                detail={'tool': name, 'available': sorted(self._tools)},
            ) from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def names(self) -> List[str]:
        return sorted(self._tools)

    def schemas_for_llm(self) -> List[Dict[str, Any]]:
        return [tool.to_schema() for tool in self.list_tools()]

    def metadata(self) -> List[Dict[str, Any]]:
        return [tool.to_public_metadata() for tool in self.list_tools()]

    def available_for(self, user) -> List[Tool]:
        from craftflow_ai.permissions.policy import has_permission

        return [t for t in self.list_tools() if has_permission(user, t.permission)]

    def __len__(self):
        return len(self._tools)

    def __contains__(self, name):
        return name in self._tools


# رجیستری سراسری (تنها نمونهٔ مورد استفاده)
registry = ToolRegistry()