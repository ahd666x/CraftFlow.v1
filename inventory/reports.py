"""
گزارش‌ها و تحلیل‌های فقط‌خواندنی مواد (Phase 10 تا 16).

قواعد مشترک این ماژول:

* ``StockMovement`` منبع حقیقت گردش انبار است و ``DailyMaterialQueue`` منبع
  برنامه و عملیات روزانه؛ هیچ‌کدام این گزارش‌ها چیزی نمی‌نویسند.
* هیچ قاعدهٔ جدیدی برای محاسبهٔ مصرف ساخته نمی‌شود؛ همهٔ اعداد از فیلدهای
  ذخیره‌شدهٔ موجود (``planned_quantity``, ``delivered_quantity``,
  ``returned_quantity``, ``actual_consumption``) خوانده می‌شوند.
* این ماژول جایگزین ``StockMovement`` نیست؛ فقط آن را نمایش می‌دهد.
"""
from decimal import Decimal

from django.db.models import Case, Count, DecimalField, F, Q, Sum, When
from django.utils import timezone

from .models import (
    DailyMaterialClosing,
    DailyMaterialQueue,
    DailyMaterialQueueSource,
    RawMaterial,
    StockMovement,
)

# علامت موجودی از تنها تعریف موجود در مدل می‌آید تا هیچ گزارشی علامت خودش را
# نسازد و یک حرکت در دو جا با دو علامت شمرده نشود.
def _signed(prefix=''):
    """جمع علامت‌دار مقدار حرکت‌ها با پیشوند رابطه (مثلاً ``movements__``)."""
    quantity = F(f'{prefix}quantity')
    return Case(
        When(**{f'{prefix}movement_type__in': RawMaterial.STOCK_DECREASING_TYPES},
             then=-quantity),
        default=quantity,
        output_field=DecimalField(),
    )


_SIGNED_TOTAL = _signed()

# سقف نمایش برای گزارش‌های ریز تا مرورگر در کارگاه سنگین نشود.
LEDGER_ROW_LIMIT = 2000


def _q(value):
    if value is None:
        return Decimal('0.00')
    return Decimal(str(value)).quantize(Decimal('0.01'))


def _queue_url(date):
    """لینک صف روزانه برای یک تاریخ (شمسی، مثل بقیهٔ صفحات)."""
    from django.urls import reverse

    try:
        import jdatetime
        jalali = jdatetime.date.fromgregorian(date=date).strftime('%Y-%m-%d')
    except Exception:  # pragma: no cover - فقط در نبود jdatetime
        jalali = date.strftime('%Y-%m-%d')
    return f'{reverse("inventory:daily_material_queue")}?date={jalali}'


def _closing_url(date):
    from django.urls import reverse

    try:
        import jdatetime
        jalali = jdatetime.date.fromgregorian(date=date).strftime('%Y-%m-%d')
    except Exception:  # pragma: no cover
        jalali = date.strftime('%Y-%m-%d')
    return f'{reverse("inventory:daily_closing")}?date={jalali}'


# ===================================================================
#  Phase 10 — دفتر گردش مواد (Material Ledger)
# ===================================================================

def material_ledger(*, date_from=None, date_to=None, material_id=None,
                    worker_id=None, movement_type=None, order_id=None):
    """
    دفتر گردش هر ماده بر اساس ``StockMovement`` — فقط خواندنی.

    مسیر نمایش‌داده‌شده همان چرخهٔ واقعی است:
    انبار ← تحویل ← کارگر ← برگشت ← مصرف واقعی ← انبار.

    فیلترها: تاریخ (از/تا)، ماده، کارگر، نوع حرکت و سفارش.
    کارگر از رابطهٔ ``daily_queue__worker`` فیلتر می‌شود، چون هر حرکتِ تحویل و
    بازگشت به ردیف صف روزانه‌اش وصل است.
    """
    qs = StockMovement.objects.select_related(
        'raw_material', 'daily_queue', 'daily_queue__worker',
        'reference_task', 'reference_order_item', 'created_by',
    ).all()

    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    if material_id:
        qs = qs.filter(raw_material_id=material_id)
    if movement_type:
        qs = qs.filter(movement_type=movement_type)
    if worker_id:
        qs = qs.filter(daily_queue__worker_id=worker_id)
    if order_id:
        qs = qs.filter(reference_order_item__order_id=order_id)

    total_count = qs.count()
    rows = list(
        qs.order_by('raw_material__name', 'created_at', 'id')[:LEDGER_ROW_LIMIT]
    )

    # موجودی اولیهٔ هر ماده = مجموع حرکات قبل از اولین ردیفِ نمایش‌داده‌شده
    first_by_material = {}
    for movement in rows:
        first_by_material.setdefault(movement.raw_material_id, movement)

    opening = {}
    for raw_id, first in first_by_material.items():
        prior = StockMovement.objects.filter(raw_material_id=raw_id).filter(
            Q(created_at__lt=first.created_at)
            | Q(created_at=first.created_at, id__lt=first.id)
        ).aggregate(total=Sum(_SIGNED_TOTAL))
        opening[raw_id] = _q(prior['total'])

    running = dict(opening)
    lines = []
    decreasing = RawMaterial.STOCK_DECREASING_TYPES
    for movement in rows:
        amount = _q(movement.quantity)
        signed = -amount if movement.movement_type in decreasing else amount
        running[movement.raw_material_id] = _q(
            running.get(movement.raw_material_id, Decimal('0.00')) + signed
        )
        lines.append({
            'movement': movement,
            'signed': signed,
            'balance': running[movement.raw_material_id],
            'reference': _movement_reference(movement),
        })

    return {
        'rows': lines,
        'total_count': total_count,
        'truncated': total_count > len(rows),
        'limit': LEDGER_ROW_LIMIT,
        'materials': RawMaterial.objects.filter(is_active=True).order_by('name'),
    }


def _movement_reference(movement):
    """مرجع خوانا برای یک حرکت انبار."""
    if movement.reference_order_item_id:
        return f"سفارش #{movement.reference_order_item.order_id} / آیتم #{movement.reference_order_item_id}"
    if movement.reference_task_id:
        return f"تسک #{movement.reference_task_id}"
    if movement.daily_queue_id:
        queue = movement.daily_queue
        if queue is not None:
            return (
                f"صف #{queue.pk} — {queue.work_date} / "
                f"{queue.raw_material.name}"
            )
        return f"صف #{movement.daily_queue_id}"
    return movement.note or '—'


# ===================================================================
#  Phase 11 — تحلیل مصرف (Consumption Analysis)
# ===================================================================

def consumption_report(*, date=None, date_from=None, date_to=None,
                       material_id=None, worker_id=None):
    """
    Planned / Delivered / Returned / Actual / Variance برای ماده و کارگر.

    هیچ مصرفی دوباره محاسبه نمی‌شود: ``actual_consumption`` از همان فیلد
    ذخیره‌شدهٔ صف خوانده می‌شود. ``variance`` صرفاً اختلاف عددی
    ``actual - planned`` است، نه قاعدهٔ مصرف تازه.
    """
    qs = DailyMaterialQueue.objects.filter(status__in=('pending', 'partial', 'delivered', 'returned'))
    if date:
        qs = qs.filter(work_date=date)
    if date_from:
        qs = qs.filter(work_date__gte=date_from)
    if date_to:
        qs = qs.filter(work_date__lte=date_to)
    if material_id:
        qs = qs.filter(raw_material_id=material_id)
    if worker_id:
        qs = qs.filter(worker_id=worker_id)

    grouped = (
        qs.values('raw_material_id', 'raw_material__name', 'raw_material__unit')
        .annotate(
            planned=Sum('planned_quantity'),
            delivered=Sum('delivered_quantity'),
            returned=Sum('returned_quantity'),
            actual=Sum('actual_consumption'),
            excess=Sum('excess_consumption'),
            conflicts=Count('id', filter=Q(has_plan_conflict=True)),
        )
        .order_by('raw_material__name')
    )

    rows = []
    totals = {'planned': Decimal('0'), 'delivered': Decimal('0'),
              'returned': Decimal('0'), 'actual': Decimal('0'),
              'excess': Decimal('0'), 'conflicts': 0}
    for item in grouped:
        planned = _q(item['planned'])
        actual = _q(item['actual'])
        row = {
            'raw_material_id': item['raw_material_id'],
            'raw_material': item['raw_material__name'],
            'unit': item['raw_material__unit'],
            'planned': planned,
            'delivered': _q(item['delivered']),
            'returned': _q(item['returned']),
            'actual': actual,
            'excess': _q(item['excess']),
            # اختلاف سادهٔ عددی، نه قاعدهٔ مصرف جدید
            'variance': _q(actual - planned),
            'conflicts': item['conflicts'] or 0,
        }
        rows.append(row)
        for key in ('planned', 'delivered', 'returned', 'actual', 'excess'):
            totals[key] += row[key]
        totals['conflicts'] += row['conflicts']

    totals['variance'] = _q(totals['actual'] - totals['planned'])
    return {'rows': rows, 'totals': totals}


# ===================================================================
#  Phase 12 — ردیابی مواد سفارش (Order Material Traceability)
# ===================================================================

def order_material_traceability(order):
    """
    مسیر کامل ماده از سفارش تا مصرف، فقط با روابط موجود.

    Order → ProductionTask → DailyMaterialQueueSource → DailyMaterialQueue
    → (تحویل/برگشت) و برای حرکت انبار → StockMovement

    ``DailyMaterialQueue`` برای هر تسک از راه ``sources`` پیدا می‌شود، پس
    هیچ مدل و فیلد جدیدی لازم نیست.
    """
    task_ids = list(
        order.items.values_list('id', flat=True)
    )
    if not task_ids:
        return {'rows': [], 'totals': None, 'movements': []}

    task_id_set = set(task_ids)
    queues = list(
        DailyMaterialQueue.objects
        .filter(sources__production_task__order_item_id__in=task_ids)
        .select_related('worker', 'raw_material', 'painting_stage')
        .prefetch_related(
            'sources__production_task',
        )
        .distinct()
    )

    per_material_stage = {}
    for queue in queues:
        planned = _q(queue.planned_quantity)
        key = (queue.raw_material_id, queue.painting_stage_id)
        entry = per_material_stage.get(key)
        if entry is None:
            entry = per_material_stage[key] = {
                'raw_material_id': queue.raw_material_id,
                'raw_material': queue.raw_material.name,
                'unit': queue.raw_material.get_unit_display(),
                'stage_id': queue.painting_stage_id,
                'stage_name': queue.painting_stage.name if queue.painting_stage else '—',
                'planned': Decimal('0.00'),
                'delivered': Decimal('0.00'),
                'returned': Decimal('0.00'),
                'actual': Decimal('0.00'),
                'statuses': set(),
                'conflicts': 0,
                'tasks': set(),
                'workers': set(),
            }

        # سهم همین سفارش از نیاز آن ردیف صف (یک بار برای هر ردیف).
        # منابعِ جبران خرابی به تسک وصل نیستند و سفارششان از راه خرابی معلوم
        # است، پس اینجا شمرده نمی‌شوند تا نیاز سفارش دو بار حساب نشود.
        mine = [
            source for source in queue.sources.all()
            if source.production_task_id
            and source.production_task.order_item_id in task_id_set
        ]
        share = _q(sum((source.quantity for source in mine), Decimal('0.00')))
        entry['planned'] += share

        # سهم این سفارش از تحویل/برگشت/مصرف به نسبت سهم از نیاز
        if planned:
            ratio = planned
            entry['delivered'] += _q(_q(queue.delivered_quantity) / ratio * share)
            entry['returned'] += _q(_q(queue.returned_quantity) / ratio * share)
            entry['actual'] += _q(_q(queue.actual_consumption) / ratio * share)

        entry['statuses'].add(queue.status)
        entry['conflicts'] += 1 if queue.has_plan_conflict else 0
        for source in mine:
            entry['tasks'].add(source.production_task_id)
        if queue.worker_id:
            entry['workers'].add(queue.worker_id)

    rows = []
    for entry in sorted(per_material_stage.values(), key=lambda e: (e['raw_material'], e['stage_name'])):
        rows.append({
            **entry,
            'status': 'conflict' if entry['conflicts'] else _merge_status(entry['statuses']),
            'task_count': len(entry['tasks']),
        })

    movements = StockMovement.objects.filter(
        reference_order_item__order_id=order.id
    ).select_related('raw_material')

    totals = {
        'planned': sum((r['planned'] for r in rows), Decimal('0.00')),
        'delivered': sum((r['delivered'] for r in rows), Decimal('0.00')),
        'returned': sum((r['returned'] for r in rows), Decimal('0.00')),
        'actual': sum((r['actual'] for r in rows), Decimal('0.00')),
        'movement_count': movements.count(),
    }

    return {
        'rows': rows,
        'totals': totals,
        'movements': movements.order_by('-created_at')[:200],
    }


def _merge_status(statuses):
    """ترکیب وضعیت چند ردیف صف برای یک ماده در سفارش."""
    if not statuses:
        return 'pending'
    if 'delivered' in statuses or 'returned' in statuses:
        return 'delivered'
    if 'pending' in statuses:
        return 'pending'
    return sorted(statuses)[0]


def low_stock_rows():
    """
    موادی که موجودی‌شان به حداقل هشدار رسیده یا از آن کمتر است — فقط خواندنی.

    موجودی در همان کوئری با ``_signed`` حساب می‌شود تا برای هر ماده یک کوئری
    جدا نرود و علامتش با ``RawMaterial.current_stock`` یکی بماند.
    """
    rows = (
        RawMaterial.objects
        .filter(is_active=True)
        .annotate(stock=Sum(_signed('movements__')))
        .order_by('stock', 'name')
    )
    out = []
    for material in rows:
        stock = _q(material.stock)
        threshold = _q(material.min_stock_alert)
        if stock > threshold:
            continue
        out.append({
            'id': material.id,
            'name': material.name,
            'code': material.code,
            'category': material.category.name if material.category_id else '',
            'unit': material.get_unit_display(),
            'stock': stock,
            'min_stock_alert': threshold,
            'shortage': _q(threshold - stock),
        })
    return out


# ===================================================================
#  Phase 13 — داشبورد برنامه‌ریزی مواد
# ===================================================================

def material_planning_dashboard(date=None):
    """داشبورد مدیریتی + بخش انبار برای یک روز (فقط خواندنی)."""
    from product.models import Order, ProductionTask

    if date is None:
        date = timezone.localdate()

    queues = DailyMaterialQueue.objects.filter(work_date=date)
    summary = consumption_report(date=date)

    # --- بخش مدیریت ---
    # ProductionTask.order با related_name='tasks' به Order وصل است
    active_orders = Order.objects.filter(
        tasks__station_name='paint', tasks__scheduled_start__date=date,
    ).distinct().count()

    painting_tasks = ProductionTask.objects.filter(
        station_name='paint', scheduled_start__date=date,
    ).count()

    conflicts = queues.filter(has_plan_conflict=True)
    incomplete = queues.filter(status='pending')

    # --- بخش انبار ---
    needed_rows = summary['rows']
    needed_material_ids = {row['raw_material_id'] for row in needed_rows}

    # موجودی در همان کوئری محاسبه می‌شود تا برای هر ماده یک کوئری جدا نرود
    stock_rows = (
        RawMaterial.objects
        .filter(is_active=True)
        .annotate(stock=Sum(_signed('movements__')))
        .order_by('name')
    )
    materials = []
    for material in stock_rows:
        stock = _q(material.stock)
        materials.append({
            'id': material.id,
            'name': material.name,
            'unit': material.get_unit_display(),
            'stock': stock,
            'min_stock_alert': _q(material.min_stock_alert),
            'low': stock <= _q(material.min_stock_alert),
            'needed_today': material.id in needed_material_ids,
        })

    # --- per worker ---
    worker_rows = (
        queues
        .values('worker_id', 'worker__first_name', 'worker__last_name', 'worker__username')
        .annotate(
            planned=Sum('planned_quantity'),
            delivered=Sum('delivered_quantity'),
            returned=Sum('returned_quantity'),
            actual=Sum('actual_consumption'),
        )
        .order_by('worker__first_name', 'worker__last_name', 'worker__username')
    )
    # شمارش تسک‌ها در یک کوئری (نه یکی به ازای هر کارگر)
    task_counts = dict(
        ProductionTask.objects
        .filter(station_name='paint', scheduled_start__date=date)
        .values_list('assigned_worker_id')
        .annotate(n=Count('id'))
    )
    workers = []
    for row in worker_rows:
        name = ' '.join(filter(None, [
            row.get('worker__first_name'), row.get('worker__last_name'),
        ])) or row.get('worker__username') or '—'
        workers.append({
            'worker_id': row['worker_id'],
            'name': name,
            'tasks': task_counts.get(row['worker_id'], 0),
            'planned': _q(row['planned']),
            'delivered': _q(row['delivered']),
            'returned': _q(row['returned']),
            'actual': _q(row['actual']),
        })

    return {
        'date': date,
        'manager': {
            'active_orders': active_orders,
            'painting_tasks': painting_tasks,
            'queue_rows': queues.count(),
            'planned': summary['totals']['planned'],
            'delivered': summary['totals']['delivered'],
            'returned': summary['totals']['returned'],
            'actual': summary['totals']['actual'],
            'conflict_count': conflicts.count(),
            'incomplete_count': incomplete.count(),
            'conflict_rows': list(
                conflicts.select_related('worker', 'raw_material')[:50]
            ),
        },
        'warehouse': {
            'materials': materials,
            'low_count': sum(1 for m in materials if m['low']),
            'needed_today': needed_rows,
            'workers': workers,
        },
    }


# ===================================================================
#  Phase 14 — هشدارهای عملیاتی
# ===================================================================

def inventory_alerts(date=None, *, limit=100):
    """
    هشدارهای عملیاتی — فقط نمایش.

    هیچ تراکنشی ساخته نمی‌شود و هیچ داده‌ای تغییر نمی‌کند؛ این تابع فقط
    فهرست هشدارها را برمی‌گرداند.
    """
    if date is None:
        date = timezone.localdate()

    alerts = []

    def add(kind, severity, title, message, url=None):
        alerts.append({
            'kind': kind, 'severity': severity, 'title': title,
            'message': message, 'url': url, 'date': date,
        })

    queues = DailyMaterialQueue.objects.filter(work_date=date).select_related(
        'worker', 'raw_material',
    )

    # 1) موجودی کمتر از حد هشدار (موجودی در همان کوئری محاسبه می‌شود)
    for material in RawMaterial.objects.filter(is_active=True).annotate(stock=Sum(_signed('movements__'))):
        stock = _q(material.stock)
        limit_value = _q(material.min_stock_alert)
        if limit_value > 0 and stock <= limit_value:
            add(
                'low_stock', 'danger', 'موجودی کم',
                f'«{material.name}» موجودی {stock} {material.get_unit_display()} '
                f'است (حد هشدار {limit_value}).',
                url=None,
            )

    # 2) تعارض برنامه
    for queue in queues.filter(has_plan_conflict=True):
        add(
            'conflict', 'danger', 'تعارض برنامه',
            f'{queue.worker} / {queue.raw_material.name}: {queue.conflict_note or "برنامه پس از تراکنش تغییر کرده"}',
            url=_queue_url(queue.work_date),
        )

    # 3) کار ناقص: برنامه‌ریزی‌شده ولی تحویل‌نشده
    for queue in queues.filter(status='pending'):
        if _q(queue.planned_quantity) <= 0:
            continue
        add(
            'incomplete', 'warning', 'ردیف ناقص',
            f'{queue.worker} / {queue.raw_material.name}: '
            f'{_q(queue.planned_quantity)} {queue.raw_material.get_unit_display()} '
            'برنامه‌ریزی شده ولی تحویل نشده.',
            url=_queue_url(queue.work_date),
        )

    # 4) تحویل انجام شده ولی برگشتی/وضعیت نهایی مشخص نیست
    for queue in queues.filter(status='delivered'):
        if _q(queue.returned_quantity) <= 0:
            add(
                'unreturned', 'warning', 'تحویل بدون برگشت نهایی',
                f'{queue.worker} / {queue.raw_material.name}: '
                f'{_q(queue.delivered_quantity)} {queue.raw_material.get_unit_display()} '
                'تحویل شده و هنوز برگشتی ثبت نشده.',
                url=_queue_url(queue.work_date),
            )

    # 5) روز هنوز بررسی و تأیید نشده
    if not DailyMaterialClosing.objects.filter(work_date=date).exists():
        add(
            'closing_required', 'info', 'بستن روز انجام نشده',
            f'روز {date} هنوز بررسی و تأیید نشده است.',
            url=_closing_url(date),
        )

    order = {'danger': 0, 'warning': 1, 'info': 2}
    alerts.sort(key=lambda a: order.get(a['severity'], 3))
    return {'date': date, 'alerts': alerts[:limit], 'total': len(alerts)}


# ===================================================================
#  Phase 15 — گزارش‌های تاریخی
# ===================================================================

HISTORICAL_REPORTS = [
    ('consumption', 'مصرف مواد'),
    ('delivery', 'تحویل مواد'),
    ('return', 'برگشت مواد'),
    ('planned_vs_actual', 'برنامه در برابر واقعی'),
    ('worker_consumption', 'مصرف کارگران'),
    ('order_consumption', 'مصرف سفارش‌ها'),
    ('closing', 'بستن روز'),
]


def historical_report(*, report='consumption', date_from=None, date_to=None,
                      material_id=None, worker_id=None, order_id=None,
                      product_id=None, color_part=None, status=None, limit=1000):
    """
    گزارش تاریخی فقط‌خواندنی. خروجی ساده: عنوان، ستون‌ها و ردیف‌ها.

    همهٔ اعداد از فیلدهای ذخیره‌شدهٔ موجود می‌آیند و هیچ قاعدهٔ تازه‌ای ساخته
    نمی‌شود.
    """
    if report not in dict(HISTORICAL_REPORTS):
        raise ValueError('گزارش ناشناخته')

    filters = {
        'date_from': date_from, 'date_to': date_to,
        'material_id': material_id, 'worker_id': worker_id,
        'order_id': order_id, 'product_id': product_id,
        'color_part': color_part, 'status': status,
    }

    if report == 'closing':
        qs = DailyMaterialClosing.objects.select_related('closed_by')
        if date_from:
            qs = qs.filter(work_date__gte=date_from)
        if date_to:
            qs = qs.filter(work_date__lte=date_to)
        return {
            'report': report,
            'title': dict(HISTORICAL_REPORTS)[report],
            'columns': ['تاریخ', 'وضعیت', 'تأییدکننده', 'زمان تأیید', 'یادداشت'],
            'rows': [
                [
                    item.work_date.strftime('%Y-%m-%d'),
                    item.get_status_display(),
                    (item.closed_by.get_full_name() if item.closed_by else '—')
                    or (item.closed_by.username if item.closed_by else '—'),
                    item.closed_at.strftime('%Y-%m-%d %H:%M'),
                    item.note or '—',
                ]
                for item in qs.order_by('-work_date')[:limit]
            ],
            'total': qs.count(),
        }

    qs = DailyMaterialQueue.objects.select_related('worker', 'raw_material')
    if date_from:
        qs = qs.filter(work_date__gte=date_from)
    if date_to:
        qs = qs.filter(work_date__lte=date_to)
    if material_id:
        qs = qs.filter(raw_material_id=material_id)
    if worker_id:
        qs = qs.filter(worker_id=worker_id)
    if status:
        qs = qs.filter(status=status)
    if order_id:
        qs = qs.filter(
            sources__production_task__order_item__order_id=order_id)
    if product_id:
        qs = qs.filter(
            sources__production_task__order_item__product_id=product_id)
    if color_part:
        qs = qs.filter(sources__production_task__color_part=color_part)
    qs = qs.distinct()

    total = qs.count()
    records = list(qs.order_by('work_date', 'worker__username', 'raw_material__name')[:limit])

    if report in ('consumption', 'planned_vs_actual'):
        columns = ['تاریخ', 'کارگر', 'ماده', 'برنامه', 'تحویل', 'برگشت',
                   'مصرف واقعی', 'اختلاف', 'وضعیت']
        rows = [[
            item.work_date.strftime('%Y-%m-%d'),
            item.worker.get_full_name() or item.worker.username,
            item.raw_material.name,
            str(_q(item.planned_quantity)),
            str(_q(item.delivered_quantity)),
            str(_q(item.returned_quantity)),
            str(_q(item.actual_consumption)),
            str(_q(_q(item.actual_consumption) - _q(item.planned_quantity))),
            item.get_status_display(),
        ] for item in records]
        title = dict(HISTORICAL_REPORTS)[report]
    elif report in ('delivery', 'return'):
        column_name = 'تحویل‌شده' if report == 'delivery' else 'برگشت‌شده'
        amount = 'delivered_quantity' if report == 'delivery' else 'returned_quantity'
        columns = ['تاریخ', 'کارگر', 'ماده', column_name, 'وضعیت']
        rows = [[
            item.work_date.strftime('%Y-%m-%d'),
            item.worker.get_full_name() or item.worker.username,
            item.raw_material.name,
            str(_q(getattr(item, amount))),
            item.get_status_display(),
        ] for item in records]
        title = dict(HISTORICAL_REPORTS)[report]
    elif report == 'worker_consumption':
        grouped = (
            qs.values('worker__first_name', 'worker__last_name', 'worker__username')
            .annotate(
                planned=Sum('planned_quantity'),
                delivered=Sum('delivered_quantity'),
                returned=Sum('returned_quantity'),
                actual=Sum('actual_consumption'),
            )
        )
        columns = ['کارگر', 'برنامه', 'تحویل', 'برگشت', 'مصرف واقعی']
        rows = [[
            ' '.join(filter(None, [row.get('worker__first_name'),
                                   row.get('worker__last_name')]))
            or row.get('worker__username') or '—',
            str(_q(row['planned'])), str(_q(row['delivered'])),
            str(_q(row['returned'])), str(_q(row['actual'])),
        ] for row in grouped]
        title = dict(HISTORICAL_REPORTS)[report]
    else:  # order_consumption
        order_ids = sorted({
            source.production_task.order_item.order_id
            for queue in records
            for source in queue.sources.select_related(
                'production_task__order_item').all()
        })
        columns = ['شماره سفارش', 'تعداد ردیف', 'برنامه', 'تحویل', 'برگشت', 'مصرف واقعی']
        rows = []
        for oid in order_ids:
            item = order_material_traceability_by_id(oid, filters)
            if not item['rows']:
                continue
            rows.append([
                f'#{oid}',
                str(len(item['rows'])),
                str(item['totals']['planned']),
                str(item['totals']['delivered']),
                str(item['totals']['returned']),
                str(item['totals']['actual']),
            ])
        title = dict(HISTORICAL_REPORTS)[report]

    return {
        'report': report, 'title': title, 'columns': columns,
        'rows': rows, 'total': total, 'filters': filters,
    }


def order_material_traceability_by_id(order_id, filters=None):
    """همان ردیابی سفارش، فقط با شمارهٔ سفارش (برای گزارش‌های تاریخی)."""
    from product.models import Order

    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        return {'rows': [], 'totals': None}
    return order_material_traceability(order)


# ===================================================================
#  Phase 16 — حسابرسی یکپارچگی داده (فقط تشخیص)
# ===================================================================

def data_integrity_audit():
    """
    تشخیص مشکلات یکپارچگی داده — هیچ داده‌ای را خودکار اصلاح نمی‌کند.

    خروجی هر مورد: problem, severity, object, reference, description.
    """
    issues = []

    def add(problem, severity, obj, reference, description):
        issues.append({
            'problem': problem, 'severity': severity, 'object': obj,
            'reference': reference, 'description': description,
        })

    queues = DailyMaterialQueue.objects.select_related('worker', 'raw_material').all()

    for queue in queues:
        label = f'صف #{queue.pk}'
        reference = f'{queue.work_date} / {queue.raw_material.name}'

        delivered = _q(queue.delivered_quantity)
        returned = _q(queue.returned_quantity)
        actual = _q(queue.actual_consumption)
        planned = _q(queue.planned_quantity)

        if returned > delivered:
            add('returned_gt_delivered', 'critical', label, reference,
                f'برگشتی {returned} بیشتر از تحویل {delivered} است.')
        if actual < 0:
            add('negative_actual', 'critical', label, reference,
                f'مصرف واقعی منفی است: {actual}.')
        if actual != _q(delivered - returned):
            add('actual_mismatch', 'critical', label, reference,
                f'actual_consumption={actual} ولی delivered−returned='
                f'{_q(delivered - returned)}.')
        if queue.status == 'cancelled' and queue.has_transaction:
            add('cancelled_with_transaction', 'critical', label, reference,
                'ردیف لغوشدهtransaction دارد (تحویل یا برگشت ثبت شده).')
        if queue.status in ('delivered', 'returned', 'partial') and not queue.has_transaction:
            add('transaction_mismatch', 'warning', label, reference,
                f'وضعیت «{queue.get_status_display()}» است ولی '
                'هیچ تحویل/برگشتی ثبت نشده.')
        if queue.status == 'returned' and returned < delivered:
            add('transaction_mismatch', 'warning', label, reference,
                'وضعیت «برگشت ثبت شده» ولی همهٔ تحویل برگشته نیست.')
        if planned < 0 or delivered < 0 or returned < 0:
            add('negative_quantity', 'critical', label, reference,
                'مقدار منفی در planned/delivered/returned.')
        if queue.worker_id is None:
            add('invalid_worker', 'warning', label, reference, 'کارگر معتبر نیست.')

    # منبع یتیم / ناسازگار با نوعش
    valid_queue_ids = set(DailyMaterialQueue.objects.values_list('id', flat=True))
    for source in DailyMaterialQueueSource.objects.select_related('queue').all():
        if source.queue_id not in valid_queue_ids:
            add('orphan_source', 'critical', f'منبع #{source.pk}',
                f'queue={source.queue_id}',
                'منبع به هیچ صفی وصل نیست.')
            continue
        # هر نوع منبع دقیقاً یک شکل دارد؛ نبودِ فیلدِ لازم یعنی ردیف ناقص.
        if source.kind == 'rework':
            if source.defect_id is None:
                add('incomplete_source', 'warning', f'منبع #{source.pk}',
                    f'queue={source.queue_id}',
                    'منبع جبران خرابی بدون خرابی است.')
        if source.kind == 'carryover':
            if source.carryover_from_id is None:
                add('incomplete_source', 'warning', f'منبع #{source.pk}',
                    f'queue={source.queue_id}',
                    'منبع انتقال کسری بدون ردیف مبدأ است.')
            continue
        elif source.kind == 'painting':
            if source.production_task_id is None or source.painting_stage_id is None:
                add('incomplete_source', 'warning', f'منبع #{source.pk}',
                    f'queue={source.queue_id}',
                    'منبع نقاشی بدون تسک یا بدون مرحلهٔ نقاشی است.')
        elif source.kind == 'station':
            if source.production_task_id is None:
                add('incomplete_source', 'warning', f'منبع #{source.pk}',
                    f'queue={source.queue_id}',
                    'منبع ایستگاه بدون تسک تولید است.')

    # مصرف بدون ردیف صف: تنها راه مجاز برای ثبت مصرف، صف روزانه است.
    for movement in (
        StockMovement.objects.filter(movement_type='consumption', daily_queue__isnull=True)
        .select_related('raw_material')[:50]
    ):
        add('orphan_consumption', 'critical', 'StockMovement',
            f'#{movement.pk} — {movement.raw_material.name} ({movement.quantity})',
            'حرکت مصرف به هیچ ردیف صف وصل نیست؛ تحویل باید فقط از صف '
            'مواد روزانه ثبت شود تا موجودی دوبار کم نشود.')

    # حرکات تکراری مشکوک
    duplicates = (
        StockMovement.objects.filter(movement_type='consumption')
        .values('raw_material_id', 'daily_queue_id', 'quantity', 'created_at__date')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
    )
    for dup in duplicates:
        add('duplicate_delivery', 'warning', 'StockMovement',
            f'raw={dup["raw_material_id"]} صف={dup["daily_queue_id"]} '
            f'qty={dup["quantity"]} تاریخ={dup["created_at__date"]}',
            f'{dup["n"]} حرکت مصرف یکسان در یک روز ثبت شده است.')

    duplicates_return = (
        StockMovement.objects.filter(movement_type='return')
        .values('raw_material_id', 'daily_queue_id', 'quantity', 'created_at__date')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
    )
    for dup in duplicates_return:
        add('duplicate_return', 'warning', 'StockMovement',
            f'raw={dup["raw_material_id"]} صف={dup["daily_queue_id"]} '
            f'qty={dup["quantity"]} تاریخ={dup["created_at__date"]}',
            f'{dup["n"]} حرکت برگشت یکسان در یک روز ثبت شده است.')

    # موجودی منفی
    for material in RawMaterial.objects.filter(is_active=True).annotate(stock=Sum(_signed('movements__'))):
        stock = _q(material.stock)
        if stock < 0:
            add('negative_stock', 'critical', f'ماده «{material.name}»', material.code,
                f'موجودی انبار منفی است: {stock}.')

    # روزهایی که بیش از یک بار بسته شده‌اند (unique_today باید جلویش را بگیرد)
    seen = {}
    for closing in DailyMaterialClosing.objects.all():
        seen.setdefault(closing.work_date, 0)
        seen[closing.work_date] += 1
    for work_date, count in seen.items():
        if count > 1:
            add('duplicate_closing', 'critical', 'DailyMaterialClosing',
                str(work_date), f'روز {count} بار بسته شده است.')

    severity_order = {'critical': 0, 'warning': 1, 'info': 2}
    issues.sort(key=lambda i: severity_order.get(i['severity'], 3))
    return {
        'issues': issues,
        'total': len(issues),
        'critical': sum(1 for i in issues if i['severity'] == 'critical'),
        'warning': sum(1 for i in issues if i['severity'] == 'warning'),
    }
