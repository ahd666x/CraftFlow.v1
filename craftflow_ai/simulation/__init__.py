"""
شبیه‌سازی خالص CraftFlow AI (فاز ۲).

مرز شبیه‌سازی عمداً سخت‌گیرانه است:

    scenarios (خواندن داده)  →  Snapshot (دادهٔ خالص)  →  engine.run (محاسبهٔ خالص)

هیچ‌جای این زنجیره چیزی در دیتابیس نوشته نمی‌شود و هیچ نمونهٔ مدل Django
تغییر نمی‌کند. این تضمین با تست‌های ``test_simulation.py`` (خلوص ایستا و
اجرایی) و ``test_simulation_determinism.py`` (قطعی‌بودن و نبود نشت نوشتن)
بررسی می‌شود.
"""
from craftflow_ai.simulation.engine import (  # noqa: F401
    SCENARIO_MATERIAL_AVAILABILITY,
    SCENARIO_ORDER_PRIORITY,
    material_availability,
    order_priority,
    run,
)
from craftflow_ai.simulation.models import (  # noqa: F401
    Diff,
    MaterialSnapshot,
    OpenIssueSnapshot,
    Projection,
    QueueChange,
    QueueEntry,
    RawMaterialSnapshot,
    Scenario,
    SimulationResult,
    Snapshot,
)
from craftflow_ai.simulation.scenarios import (  # noqa: F401
    ORDER_RELEASE,
    PRIORITY_CHOICES,
    PRODUCTION_SEQUENCE,
    STATION_CAPACITY,
    material_snapshot,
    priority_choices,
    priority_label,
    queue_snapshot,
    simulate_material_availability,
    simulate_order_priority,
)

__all__ = [
    'Diff',
    'MaterialSnapshot',
    'OpenIssueSnapshot',
    'ORDER_RELEASE',
    'PRIORITY_CHOICES',
    'PRODUCTION_SEQUENCE',
    'Projection',
    'QueueChange',
    'QueueEntry',
    'RawMaterialSnapshot',
    'SCENARIO_MATERIAL_AVAILABILITY',
    'SCENARIO_ORDER_PRIORITY',
    'STATION_CAPACITY',
    'Scenario',
    'SimulationResult',
    'Snapshot',
    'material_availability',
    'material_snapshot',
    'order_priority',
    'priority_choices',
    'priority_label',
    'queue_snapshot',
    'run',
    'simulate_material_availability',
    'simulate_order_priority',
]