"""
لایهٔ تحلیل قطعی CraftFlow AI (فاز ۲).

قانون معماری اجباری — **تحلیل هرگز ابزار import نمی‌کند**:

    craftflow_ai.analysis  →  craftflow_ai.services.queries / domain helpers  →  ORM

مسیر معکوس (``analysis → tools``) ممنوع است و در
``craftflow_ai/tests/test_analysis_permissions.py`` به‌صورت ایستا بررسی
می‌شود. تنها نقطهٔ مجازِ پل، ماژول‌های ``craftflow_ai.tools.*`` هستند که از
این لایه استفاده می‌کنند، نه برعکس.

همهٔ اعداد، وضعیت‌ها، شدت‌ها و اطمینان‌ها در این لایه و سمت سرور ساخته
می‌شوند؛ LLM فقط آن‌ها را توضیح می‌دهد.
"""
from craftflow_ai.analysis.evidence import (  # noqa: F401
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    AnalysisReport,
    DomainResult,
    Evidence,
    Finding,
    confidence_from_signals,
    insufficient_data,
    rollup_status,
)
from craftflow_ai.analysis.inventory import (  # noqa: F401
    material_impact,
    order_material_impact,
    warehouse_impact,
)
from craftflow_ai.analysis.limits import (  # noqa: F401
    LIMITS,
    STATUS_INSUFFICIENT_DATA,
    STATUS_UNKNOWN,
    limitation,
    unavailable,
)
from craftflow_ai.analysis.order import (  # noqa: F401
    DELAY_CAUSE_PRECEDENCE,
    DELAY_CAUSE_SEVERITY,
    HEALTH_DOMAINS,
    order_delay,
    order_health,
)
from craftflow_ai.analysis.production import (  # noqa: F401
    STATION_CODES,
    STATION_LABELS,
    station_bottleneck,
    unknown_station_bucket,
)

__all__ = [
    'AnalysisReport',
    'CONFIDENCE_HIGH',
    'CONFIDENCE_LOW',
    'CONFIDENCE_MEDIUM',
    'DELAY_CAUSE_PRECEDENCE',
    'DELAY_CAUSE_SEVERITY',
    'DomainResult',
    'Evidence',
    'Finding',
    'HEALTH_DOMAINS',
    'LIMITS',
    'STATION_CODES',
    'STATION_LABELS',
    'STATUS_INSUFFICIENT_DATA',
    'STATUS_UNKNOWN',
    'confidence_from_signals',
    'insufficient_data',
    'limitation',
    'material_impact',
    'order_delay',
    'order_health',
    'order_material_impact',
    'rollup_status',
    'station_bottleneck',
    'unavailable',
    'unknown_station_bucket',
    'warehouse_impact',
]