"""
تنظیمات لایهٔ هوش مصنوعی.

همهٔ مقادیر از ``settings`` خوانده می‌شوند و در ``selvi/settings.py`` از
Environment Variable گرفته می‌شوند (با ``python-decouple``)، بنابراین هیچ
API Key وارد Git نمی‌شود.

هیچ Provider جدیدی برای فعال‌شدن لازم نیست؛ فقط کافی است نام آن در
``CRAFTFLOW_AI_PROVIDER`` قرار گیرد و در ``craftflow_ai.providers`` ثبت شود.
"""
from django.conf import settings


DEFAULTS = {
    'CRAFTFLOW_AI_ENABLED': True,
    'CRAFTFLOW_AI_PROVIDER': 'gemini',
    'CRAFTFLOW_AI_MODEL': 'gemini-2.5-flash',
    'CRAFTFLOW_AI_API_KEY': '',
    'CRAFTFLOW_AI_BASE_URL': '',
    'CRAFTFLOW_AI_MAX_TOOL_CALLS': 8,
    'CRAFTFLOW_AI_TIMEOUT': 60,
    'CRAFTFLOW_AI_MAX_HISTORY': 10,
    'CRAFTFLOW_AI_TEMPERATURE': 0.0,
}


def _get(name):
    default = DEFAULTS[name]
    if hasattr(settings, name):
        return getattr(settings, name)
    return default


def is_enabled():
    return bool(_get('CRAFTFLOW_AI_ENABLED'))


def get_provider_name():
    return (_get('CRAFTFLOW_AI_PROVIDER') or '').strip().lower()


def get_model():
    return (_get('CRAFTFLOW_AI_MODEL') or '').strip()


def get_api_key():
    return (_get('CRAFTFLOW_AI_API_KEY') or '').strip()


def get_base_url():
    return (_get('CRAFTFLOW_AI_BASE_URL') or '').strip()


def get_max_tool_calls():
    try:
        return max(1, int(_get('CRAFTFLOW_AI_MAX_TOOL_CALLS')))
    except (TypeError, ValueError):
        return DEFAULTS['CRAFTFLOW_AI_MAX_TOOL_CALLS']


def get_timeout():
    try:
        return max(1, int(_get('CRAFTFLOW_AI_TIMEOUT')))
    except (TypeError, ValueError):
        return DEFAULTS['CRAFTFLOW_AI_TIMEOUT']


def get_max_history():
    try:
        return max(0, int(_get('CRAFTFLOW_AI_MAX_HISTORY')))
    except (TypeError, ValueError):
        return DEFAULTS['CRAFTFLOW_AI_MAX_HISTORY']


def get_temperature():
    try:
        return float(_get('CRAFTFLOW_AI_TEMPERATURE'))
    except (TypeError, ValueError):
        return DEFAULTS['CRAFTFLOW_AI_TEMPERATURE']


def public_config():
    """تنظیمات قابل نمایش به لایهٔ UI — بدون هیچ اطلاعات محرمانه."""
    return {
        'enabled': is_enabled(),
        'provider': get_provider_name(),
        'model': get_model(),
        'has_api_key': bool(get_api_key()),
    }