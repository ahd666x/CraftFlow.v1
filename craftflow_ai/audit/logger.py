"""
ثبت رد ممیزی.

دو نکتهٔ امنیتی که رعایت شده است:

1. **محرمانگی** — در آرگومان‌ها و نتیجهٔ ذخیره‌شده هیچ کلید، توکن یا
   ``Authorization`` header نوشته نمی‌شود. تابع ``_sanitize`` هر کلید حساس
   را حذف می‌کند.
2. **بی‌اثر بودن بر جریان اصلی** — خطای ثبت (مثلاً نبود جدول در محیطی که
   migrate نشده) هرگز نباید پاسخ کاربر را از کار بیندازد، پس در ``record``
   همه‌چیز در ``try`` است.
"""
import logging
import time

logger = logging.getLogger('craftflow_ai.audit')

SENSITIVE_KEYS = frozenset({
    'api_key', 'apikey', 'authorization', 'token', 'password', 'secret',
    'access_token', 'refresh_token', 'x-goog-api-key', 'bearer',
})

MAX_STORED_RESULT_CHARS = 4000


def _sanitize(value, depth=0):
    """حذف کلیدهای حساس و محدود کردن عمق/اندازهٔ ساختار ذخیره‌شده."""
    if depth > 6:
        return '<truncated>'
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if str(key).strip().lower() in SENSITIVE_KEYS:
                continue
            clean[key] = _sanitize(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1) for item in list(value)[:200]]
    if isinstance(value, str) and len(value) > MAX_STORED_RESULT_CHARS:
        return value[:MAX_STORED_RESULT_CHARS] + '…'
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_STORED_RESULT_CHARS]


class ToolCallRecorder:
    """context manager برای اندازه‌گیری و ثبت یک فراخوانی ابزار."""

    def __init__(self, tool_name, arguments, user=None, session_id=''):
        self.tool_name = tool_name
        self.arguments = arguments or {}
        self.user = user if (user and getattr(user, 'pk', None)) else None
        self.session_id = session_id or ''
        self._start = None

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        duration_ms = int((time.monotonic() - (self._start or time.monotonic())) * 1000)
        if exc is None:
            record(
                tool_name=self.tool_name,
                arguments=self.arguments,
                user=self.user,
                session_id=self.session_id,
                status='completed',
                duration_ms=duration_ms,
            )
        elif getattr(exc, 'code', '') == 'PERMISSION_DENIED':
            record(
                tool_name=self.tool_name,
                arguments=self.arguments,
                user=self.user,
                session_id=self.session_id,
                status='denied',
                error_code=exc.code,
                error_message=str(exc),
                duration_ms=duration_ms,
            )
        else:
            record(
                tool_name=self.tool_name,
                arguments=self.arguments,
                user=self.user,
                session_id=self.session_id,
                status='failed',
                error_code=getattr(exc, 'code', type(exc).__name__),
                error_message=str(exc),
                duration_ms=duration_ms,
            )
        return False


def record(tool_name, arguments=None, user=None, session_id='', status='completed',
           result=None, error_code='', error_message='', duration_ms=0):
    """ثبت یک رد ممیزی. هرگز استثنا پرتاب نمی‌کند."""
    try:
        from craftflow_ai.models import AIAuditLog

        payload = {
            'tool_name': (tool_name or 'unknown')[:64],
            'arguments': _sanitize(arguments or {}),
            'session_id': (session_id or '')[:64],
            'status': status if status in ('completed', 'failed', 'denied') else 'failed',
            'duration_ms': int(duration_ms or 0),
        }
        if result is not None:
            payload['result'] = _sanitize(result)
        if error_code:
            payload['error_code'] = str(error_code)[:48]
        if error_message:
            payload['result'] = {
                **(payload.get('result') or {}),
                'error_message': str(error_message)[:500],
            }

        return AIAuditLog.objects.create(user=user, **payload)
    except Exception:
        # ثبت ممیزی نباید جریان اصلی را متوقف کند.
        logger.exception('AI audit log failed for tool=%s', tool_name)
        return None


def record_result(tool_name, arguments, result, user=None, session_id='', duration_ms=0):
    """ثبت مختصر نتیجهٔ ساختاریافتهٔ یک ابزار (فقط کدها، نه کل داده)."""
    if isinstance(result, dict):
        if result.get('success'):
            data = result.get('data') or {}
            summary = {
                'success': True,
                'keys': sorted(data.keys())[:20] if isinstance(data, dict) else None,
            }
            if isinstance(data, dict) and isinstance(data.get('total'), int):
                summary['total'] = data['total']
            for key in ('total', 'total_open_orders', 'total_delayed',
                        'shortage_count', 'total_materials'):
                if key in data:
                    summary[key] = data[key]
            status = 'completed'
        else:
            summary = {
                'success': False,
                'error': (result.get('error') or {}).get('code', 'UNKNOWN'),
            }
            status = 'failed'
    else:
        summary = {'success': bool(result)}
        status = 'completed'

    return record(
        tool_name=tool_name,
        arguments=arguments,
        user=user,
        session_id=session_id,
        status=status,
        result=summary,
        duration_ms=duration_ms,
    )


def record_conversation_event(user, conversation_id, provider, model, tool_names=None,
                              error_code=''):
    """رد کلی یک نوبت گفتگو، بدون ذخیرهٔ متن پیام‌ها."""
    return record(
        tool_name='__chat__',
        arguments={
            'provider': provider,
            'model': model,
            'tools_called': list(tool_names or []),
        },
        user=user,
        session_id=conversation_id,
        status='failed' if error_code else 'completed',
        error_code=error_code,
    )