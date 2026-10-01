"""ابزارهای کیفیت — همه فقط-خواندنی."""
from craftflow_ai.tools import reports


def register(registry):
    reports.register_quality(registry)