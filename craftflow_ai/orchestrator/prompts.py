"""بارگذاری System Prompt."""
import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger('craftflow_ai.orchestrator.prompts')

PROMPT_PATH = Path(__file__).resolve().parent.parent / 'prompts' / 'manager.txt'

_cache = {}


def get_system_prompt():
    """
    System Prompt ذخیره‌شده.

    اگر مسیر سفارشی در ``CRAFTFLOW_AI_SYSTEM_PROMPT_PATH`` تنظیم شده باشد،
    از آن خوانده می‌شود تا بتوان prompt را بدون تغییر کد بازبینی کرد.
    """
    custom = getattr(settings, 'CRAFTFLOW_AI_SYSTEM_PROMPT_PATH', None)
    path = Path(custom) if custom else PROMPT_PATH

    key = str(path)
    if key in _cache:
        return _cache[key]

    try:
        text = path.read_text(encoding='utf-8').strip()
    except OSError:
        logger.exception('System prompt could not be read from %s', path)
        text = (
            'تو دستیار عملیاتی CraftFlow هستی. فقط بر اساس داده‌های ابزارها پاسخ بده '
            'و هیچ داده‌ای را حدس نزن.'
        )

    _cache[key] = text
    return text


def clear_cache():
    _cache.clear()