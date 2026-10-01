"""
نقطهٔ ورود لایهٔ ابزار.

هر ماژول ابزار یک تابع ``register(registry)`` دارد. ثبت‌ها idempotent هستند تا
اگر این ماژول چند بار import شد، ثبت تکراری رخ ندهد.
"""
from craftflow_ai.tools.registry import Tool, ToolRegistry, fail, ok, registry

_REGISTERED = False

_MODULES = (
    'craftflow_ai.tools.orders',
    'craftflow_ai.tools.production',
    'craftflow_ai.tools.inventory',
    'craftflow_ai.tools.reports',
    'craftflow_ai.tools.planning',
    'craftflow_ai.tools.quality',
    # فاز ۲ — تحلیل قطعی و شبیه‌سازی خالص (همچنان فقط‌خواندنی)
    'craftflow_ai.tools.analysis',
    'craftflow_ai.tools.simulation',
)


def get_registry():
    """رجیستری سراسری، با تمام ابزارهای ثبت‌شده."""
    global _REGISTERED
    if not _REGISTERED:
        register_all(registry)
        _REGISTERED = True
    return registry


class _IdempotentProxy:
    """
    پروکسی رجیستری که ثبت تکراری را بی‌صدا نادیده می‌گیرد.

    ``ToolRegistry.register`` عمداً ثبت تکراری را رد می‌کند (چون بازنویسی یک
    ابزار موجود می‌تواند مرز امنیتی را دور بزند). برای اینکه ``register_all``
    مطابق قراردادش idempotent بماند، ثبت‌های تکراری از این مسیر *نادیده* می‌شوند
    و تعریف اصلی دست‌نخورده باقی می‌ماند.
    """

    def __init__(self, target):
        self._target = target

    def register(self, tool):
        if self._target.has(tool.name):
            return tool
        return self._target.register(tool)

    def __getattr__(self, item):
        return getattr(self._target, item)

    def __len__(self):
        # متدهای جادویی از روی نوع صدا زده می‌شوند، نه __getattr__.
        return len(self._target)


def register_all(target_registry=None):
    """
    ثبت همهٔ ابزارها روی یک رجیستری (برای تست یا رجیستری سفارشی).

    این تابع idempotent است: فراخوانی دوباره روی همان رجیستری نه خطا می‌دهد و
    نه تعریف ابزارهای موجود را بازنویسی می‌کند.
    """
    from importlib import import_module

    # نکته: رجیستری خالی falsy است، پس نباید از ``or`` استفاده کرد؛ وگرنه
    # فراخوانی با یک رجیستری تازه به‌جای رجیستری سراسری ثبت می‌شد.
    if target_registry is None:
        target_registry = registry
    proxy = _IdempotentProxy(target_registry)
    for module_path in _MODULES:
        module = import_module(module_path)
        module.register(proxy)
    return target_registry


__all__ = [
    'Tool',
    'ToolRegistry',
    'fail',
    'get_registry',
    'ok',
    'register_all',
    'registry',
]