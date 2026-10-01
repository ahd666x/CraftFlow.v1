"""
سناریوهای شبیه‌سازی فاز ۲ — نقطهٔ اتصال دادهٔ واقعی به موتور خالص.

این ماژول تنها جایی است که شبیه‌سازی دیتابیس را **می‌خواند** و آن را به یک
``Snapshot`` خالص تبدیل می‌کند. خودِ موتور (``engine.run``) هیچ دسترسی به ORM
ندارد و هیچ نوشتنی انجام نمی‌شود.

قابلیت‌هایی که عمداً پیاده‌سازی **نشده‌اند** و به‌صراحت unavailable اعلام
می‌شوند (فاز ۳ یا نیازمند تصمیم معماری):

* ``simulate_order_release``   — منطق پویای ساخت Part/Task را بدون mutation نمی‌توان
  بازتولید کرد؛ شمارش تسک ساختگی ممنوع است.
* ``simulate_station_capacity`` / ``simulate_worker_availability`` — ظرفیت و مدت‌زمان
  ایستگاه‌های غیرنقاشی اصلاً در CraftFlow ذخیره نشده است.
* ``simulate_production_sequence`` — گراف وابستگی وجود ندارد.
"""
from decimal import Decimal

import jdatetime
from django.db.models import Count, Q
from django.utils import timezone

from craftflow_ai.analysis.limits import unavailable
from craftflow_ai.simulation.engine import (
    SCENARIO_MATERIAL_AVAILABILITY,
    SCENARIO_ORDER_PRIORITY,
    run,
)
from craftflow_ai.simulation.models import (
    MaterialSnapshot,
    OpenIssueSnapshot,
    QueueEntry,
    RawMaterialSnapshot,
    Scenario,
    Snapshot,
    q2,
)

from inventory.models import MaterialIssue, MaterialLeftover, RawMaterial
from product.models import Order, ProductionTask

OPEN_ISSUE_STATUSES = ('requested', 'partial')

# اولویت‌های معتبر همان مقادیر فیلد Order.priority در CraftFlow هستند.
PRIORITY_CHOICES = tuple(
    value for value, _label in (Order._meta.get_field('priority').choices or ())
)
PRIORITY_LABELS = dict(Order._meta.get_field('priority').choices or {})


# ----------------------------------------------------------------------
# قابلیت‌های عمداً غیرقابل محاسبه
# ----------------------------------------------------------------------

ORDER_RELEASE = unavailable(
    'missing_dependency_graph',
    status='unavailable',
    detail=(
        'شبیه‌سازی ورود سفارش به تولید به منطق پویای ساخت Part و Task در '
        'Order.generate_tasks وابسته است که بدون mutation قابل بازتولید نیست؛ '
        'ساخت شمارش تسک فرضی در فاز ۲ مجاز نیست.'
    ),
    extra_blocks=('order_release', 'generated_task_count'),
)

STATION_CAPACITY = unavailable(
    'missing_station_capacity',
    status='unavailable',
    detail=(
        'ظرفیت و مدت‌زمان ایستگاه‌های غیرنقاشی در CraftFlow ذخیره نشده است؛ '
        'فرض‌هایی مثل «۸ ساعت کاری در روز» ساختگی‌اند.'
    ),
    extra_blocks=('station_capacity_simulation', 'worker_availability_simulation'),
)

PRODUCTION_SEQUENCE = unavailable(
    'missing_dependency_graph',
    status='unavailable',
    detail=(
        'ProductionTask هیچ depends_on ندارد و گراف وابستگی واقعی وجود ندارد؛ '
        'بهینه‌سازی ترتیب تولید در فاز ۲ ممکن نیست.'
    ),
    extra_blocks=('production_sequence_optimization', 'critical_path'),
)


# ----------------------------------------------------------------------
# ساخت snapshot از دادهٔ واقعی (فقط‌خواندنی)
# ----------------------------------------------------------------------

def material_snapshot(raw_material_id) -> Snapshot:
    """snapshot موجودی، باقی‌ماندهٔ سالن و درخواست‌های باز یک ماده."""
    raw = (
        RawMaterial.objects
        .filter(pk=raw_material_id)
        .first()
    )
    materials = ()
    if raw is not None:
        stock = raw.current_stock or Decimal('0')
        leftover = (
            MaterialLeftover.objects
            .filter(raw_material_id=raw.pk)
            .values_list('quantity', flat=True)
            .first()
        ) or Decimal('0')
        issues = tuple(
            OpenIssueSnapshot(pk=issue.pk, remaining_quantity=q2(
                (issue.requested_quantity or Decimal('0'))
                - (issue.issued_quantity or Decimal('0'))
            ))
            for issue in MaterialIssue.objects
            .filter(raw_material_id=raw.pk, status__in=OPEN_ISSUE_STATUSES)
            .order_by('id')
        )
        materials = (MaterialSnapshot(
            raw=RawMaterialSnapshot(
                pk=raw.pk,
                name=raw.name,
                unit=raw.get_unit_display(),
                pack_size=q2(raw.pack_size or 0),
                current_stock=q2(stock),
                min_stock_alert=q2(raw.min_stock_alert or 0),
            ),
            leftover=q2(leftover),
            issues=issues,
        ),)

    return Snapshot(
        kind=SCENARIO_MATERIAL_AVAILABILITY,
        materials=materials,
        captured_at=timezone.now().isoformat(),
    )


def queue_snapshot() -> Snapshot:
    """
    snapshot صف سفارش‌های باز.

    همان تعریف «باز» در ``craftflow_ai.services.queries.open_orders``:
    هر وضعیتی جز ``completed``.
    """
    active = [code for code, _label in Order.ORDER_STATUS if code != 'completed']
    open_counts = dict(
        ProductionTask.objects
        .filter(order__status__in=active, status__in=('pending', 'waiting'))
        .values('order_id')
        .annotate(cnt=Count('id'))
        .values_list('order_id', 'cnt')
    )

    queue = []
    rows = (
        Order.objects
        .filter(status__in=active)
        .order_by('priority', 'id')
        .values('id', 'number', 'priority', 'created_at')
    )
    for row in rows:
        created = row['created_at']
        queue.append(QueueEntry(
            order_id=row['id'],
            number=row['number'] or None,
            priority=int(row['priority']),
            created_at=created.isoformat() if created else None,
            open_tasks=int(open_counts.get(row['id'], 0)),
        ))

    return Snapshot(
        kind=SCENARIO_ORDER_PRIORITY,
        queue=tuple(queue),
        captured_at=timezone.now().isoformat(),
    )


# ----------------------------------------------------------------------
# سناریوهای عمومی
# ----------------------------------------------------------------------

def simulate_material_availability(raw_material_id, additional_quantity=0):
    """«اگر X واحد اضافه شود چه می‌شود؟» — بدون هیچ نوشتنی."""
    snapshot = material_snapshot(raw_material_id)
    scenario = Scenario(
        kind=SCENARIO_MATERIAL_AVAILABILITY,
        params={
            'raw_material_id': int(raw_material_id),
            'additional_quantity': str(additional_quantity or 0),
        },
    )
    return run(snapshot, scenario)


def simulate_order_priority(order_id, new_priority):
    """«اگر اولویت این سفارش Y شود، جای آن در صف کجا می‌شود؟»"""
    if order_id is None or new_priority is None:
        return run(Snapshot(kind=SCENARIO_ORDER_PRIORITY), Scenario(
            kind=SCENARIO_ORDER_PRIORITY,
            params={'order_id': order_id, 'new_priority': new_priority},
        ))
    snapshot = queue_snapshot()
    scenario = Scenario(
        kind=SCENARIO_ORDER_PRIORITY,
        params={'order_id': int(order_id), 'new_priority': int(new_priority)},
    )
    return run(snapshot, scenario)


def priority_choices():
    return [{'value': value, 'label': PRIORITY_LABELS.get(value)} for value in PRIORITY_CHOICES]


def priority_label(value):
    return PRIORITY_LABELS.get(int(value))


__all__ = [
    'ORDER_RELEASE',
    'PRIORITY_CHOICES',
    'PRIORITY_LABELS',
    'PRODUCTION_SEQUENCE',
    'STATION_CAPACITY',
    'material_snapshot',
    'priority_choices',
    'priority_label',
    'queue_snapshot',
    'simulate_material_availability',
    'simulate_order_priority',
]