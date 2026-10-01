"""
Provider محلی بدون شبکه — برای تست و حالت آفلاین.

``LocalProvider`` یک LLM واقعی نیست: کلمه‌به‌کلمه از روی شواهد موجود
پاسخ می‌سازد. وجودش دو دلیل دارد:

1. کل تست‌های لایهٔ AI بدون اینترنت و بدون API Key اجرا می‌شوند.
2. اگر Provider اصلی پیکربندی نشده باشد، سیستم به‌جای ۵۰۳ مبهم یک پاسخ
   صریح می‌دهد که «داده‌ای وجود ندارد / دادهٔ زیر را ببینید».
"""
import re

from craftflow_ai.permissions.errors import AIConfigurationError
from craftflow_ai.providers.base import AIProvider, AIResponse, ToolCall

# نگاشت عبارت فارسی/انگلیسی به Tool — عمداً محدود و صریح (نه یک مدل زبانی).
_RULES = (
    (re.compile(r'تولید|وضعیت امروز|وضعیت کلی|گزارش تولید|production'), 'get_production_status'),
    (re.compile(r'گلوگاه|تراکم|بیشترین|بوتلنک|bottleneck'), 'find_production_bottlenecks'),
    (re.compile(r'عقب|تأخیر|تاخیر|delayed|دیرکرد'), 'find_delayed_orders'),
    (re.compile(r'کمبود|کم دار|موجودی|انبار|inventory|shortage'), 'get_inventory_status'),
    (re.compile(r'سفارش|order'), 'get_open_orders'),
)

_ORDER_ID = re.compile(r'\d+')


class LocalProvider(AIProvider):
    """پاسخ‌دهندهٔ قطعی مبتنی بر شواهد. هیچ داده‌ای اختراع نمی‌کند."""
    name = 'local'

    def __init__(self, model='local-evidence'):
        self.model = model

    def healthcheck(self):
        return {'provider': self.name, 'model': self.model, 'configured': True, 'offline': True}

    def _select_tool(self, text, tools):
        available = {t['name'] for t in (tools or [])}
        for pattern, tool_name in _RULES:
            if pattern.search(text) and tool_name in available:
                return tool_name
        return None

    def chat(self, messages, tools=None, **kwargs):
        user_text = ''
        evidence = []
        for message in reversed(messages):
            role = message.get('role')
            content = message.get('content') or ''
            if role == 'user' and 'نشانه‌های' not in content:
                user_text = content
            if role == 'tool' or 'نشانه‌های' in content:
                evidence.append(content)

        tool_name = self._select_tool(user_text, tools)
        if tool_name and not evidence:
            arguments = {}
            if tool_name in ('get_order_details', 'get_material_requirements', 'get_order_timeline'):
                match = _ORDER_ID.search(user_text)
                if match:
                    arguments = {'order_id': int(match.group())}
            return AIResponse(
                content='',
                tool_calls=[ToolCall(name=tool_name, arguments=arguments, call_id='local_1')],
                finish_reason='tool_use',
                provider=self.name,
                model=self.model,
            )

        if not evidence:
            return AIResponse(
                content=(
                    'برای پاسخ به این پرسش باید ابزارهای CraftFlow اجرا شوند، اما '
                    'Provider محلی فقط ابزار را انتخاب می‌کند و خودش داده نمی‌سازد. '
                    'لطفاً از حالت Provider واقعی استفاده کنید.'
                ),
                finish_reason='stop',
                provider=self.name,
                model=self.model,
            )

        return AIResponse(
            content=_summarize(evidence),
            finish_reason='stop',
            provider=self.name,
            model=self.model,
        )


class FakeAIProvider(AIProvider):
    """
    Provider جعلی کاملاً قابل کنترل برای تست.

    با ``responses`` برنامه‌ریزی می‌شود: هر عضو یک ``AIResponse`` یا یک
    ``exception`` است. تست‌ها با این کلاس می‌توانند خطای timeout، ابزار نامعتبر
    یا فراخوانی بیش‌ازحد را هم شبیه‌سازی کنند.
    """

    name = 'fake'

    def __init__(self, responses=None, model='fake-model'):
        self.responses = list(responses or [])
        self.model = model
        self.calls = []

    def healthcheck(self):
        return {'provider': self.name, 'model': self.model, 'configured': True, 'fake': True}

    def queue(self, response):
        self.responses.append(response)
        return self

    def queue_tool_call(self, name, arguments=None, call_id=None):
        return self.queue(AIResponse(
            content='',
            tool_calls=[ToolCall(name=name, arguments=arguments or {}, call_id=call_id)],
            finish_reason='tool_use',
            provider=self.name,
            model=self.model,
        ))

    def queue_text(self, text):
        return self.queue(AIResponse(content=text, finish_reason='stop',
                                     provider=self.name, model=self.model))

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append({'messages': list(messages), 'tools': tools, 'kwargs': kwargs})
        if not self.responses:
            return AIResponse(
                content='',
                finish_reason='stop',
                provider=self.name,
                model=self.model,
            )
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if callable(nxt):
            nxt = nxt(messages, tools, kwargs)
        nxt.provider = self.name
        nxt.model = self.model
        return nxt


def _summarize(evidence_blocks):
    """
    خلاصهٔ فشردهٔ شواهد بدون هیچ تفسیری.

    نتیجهٔ ابزارها به‌صورت JSON تک‌خطی به مدل داده می‌شود، پس ابتدا باید
    باز شود تا محتوای واقعی (نه فقط کلیدها) قابل خواندن باشد.
    """
    import json as _json

    lines = []
    for block in evidence_blocks:
        payload = None
        try:
            payload = _json.loads(block)
        except (ValueError, TypeError):
            payload = None

        if isinstance(payload, dict):
            if payload.get('success') is False:
                error = payload.get('error') or {}
                lines.append(f'خطای ابزار {error.get("code")}: {error.get("message")}')
                continue
            payload = payload.get('data', payload)

        lines.extend(_render(payload).splitlines())

    deduped = []
    for line in lines:
        stripped = line.strip()
        if stripped and stripped not in deduped:
            deduped.append(stripped)

    body = '\n'.join(f'- {line}' for line in deduped[:80])
    return 'بر اساس داده‌های دریافتی از ابزارهای CraftFlow:\n' + body


def _render(value, indent=0):
    """باز کردن ساختار JSON به متن سطر-به‌سطر."""
    pad = '  ' * indent
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                rows.append(f'{pad}{key}:')
                rows.append(_render(item, indent + 1))
            else:
                rows.append(f'{pad}{key}: {item}')
        return '\n'.join(rows)
    if isinstance(value, list):
        if not value:
            return f'{pad}[]'
        rows = []
        for index, item in enumerate(value[:40]):
            rendered = _render(item, indent + 1).strip()
            rows.append(f'{pad}- {rendered}')
        if len(value) > 40:
            rows.append(f'{pad}… و {len(value) - 40} مورد دیگر')
        return '\n'.join(rows)
    return f'{pad}{value}'


def get_provider(name):
    """ساخت Provider بر اساس نام. Provider ناشناخته = خطای پیکربندی."""
    from craftflow_ai.providers.anthropic import AnthropicProvider
    from craftflow_ai.providers.gemini import GeminiProvider
    from craftflow_ai.providers.openai import OpenAIProvider

    providers = {
        'gemini': GeminiProvider,
        'google': GeminiProvider,
        'openai': OpenAIProvider,
        'anthropic': AnthropicProvider,
        'claude': AnthropicProvider,
        'local': LocalProvider,
    }
    provider_cls = providers.get((name or '').strip().lower())
    if provider_cls is None:
        raise AIConfigurationError(
            f'Provider «{name}» شناخته نشده است.',
            detail={'requested': name, 'available': sorted(set(providers))},
        )
    return provider_cls()


__all__ = ['FakeAIProvider', 'LocalProvider', 'get_provider']