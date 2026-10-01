"""کارخانهٔ Provider — تنها نقطهٔ ساخت LLM در کل پروژه."""
import logging

from craftflow_ai import config
from craftflow_ai.permissions.errors import AIConfigurationError
from craftflow_ai.providers.base import AIProvider, AIResponse, ToolCall
from craftflow_ai.providers.local import FakeAIProvider, LocalProvider, get_provider

logger = logging.getLogger('craftflow_ai.providers')

__all__ = [
    'AIProvider',
    'AIResponse',
    'FakeAIProvider',
    'LocalProvider',
    'ToolCall',
    'build_provider',
    'get_provider',
]


def build_provider(name=None):
    """
    Provider فعال را از تنظیمات می‌سازد.

    برای تست، ``name='fake'`` یا یک نمونهٔ Provider آماده مستقیم داده می‌شود.
    """
    name = name or config.get_provider_name()
    if name == 'fake':
        return FakeAIProvider()
    return get_provider(name)