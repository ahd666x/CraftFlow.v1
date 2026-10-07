"""
تحلیل مواد (فاز ۲) — تأثیر انبار روی سفارش‌های باز.

مسیری که *واقعاً* در دادهٔ فعلی CraftFlow وجود دارد:

    RawMaterial → current_stock → صف روزانه → سفارش‌های درگیر

مسیری که *وجود ندارد* و صریحاً ``insufficient_data`` برمی‌گرداند:

    Order → ProductBOM → Part → Material → RawMaterial

چون ``product.Material.raw_material`` برای همهٔ رکوردها ``NULL`` است، هر
پرسش «این سفارش چه موادی کم دارد؟» بدون ساختن داده پاسخ‌پذیر نیست.

تمام محاسبات موجودی از همان فرمول واقعی CraftFlow استفاده می‌کنند
(فقط ``movement_type='consumption'`` کم می‌کند) و کمبود با همان ریاضیات
``inventory.services.build_plans`` سنجیده می‌شود، نه با یک محاسبهٔ موازی.
"""
from decimal import Decimal

from django.db.models import (
    Case,
    Count,
    DecimalField,
    Exists,
    IntegerField,
    F,
    OuterRef,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from craftflow_ai.analysis.evidence import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SIGNAL_DERIVED,
    SIGNAL_MISSING,
    SIGNAL_REAL,
    Evidence,
    Finding,
    confidence_from_signals,
)
from craftflow_ai.analysis.limits import (
    limitation_messages,
    unavailable,
)
from craftflow_ai.analysis.production import OPEN_STATUSES

from inventory.models import (
    DailyMaterialQueue,
    RawMaterial,
    StockMovement,
)
from product.models import (
    Material,
    Order,
    PaintingMaterialRequirement,
    ProductionTask,
)

MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)
ZERO = Decimal('0.00')

OPEN_ISSUE_STATUSES = ('pending', 'delivered')
OPEN_DEFECT_STATUSES = ('reported', 'material_requested')

MAX_ORDER_IDS = 50


def money(value):
    """قالب‌بندی امن Decimal برای خروجی JSON (هم‌رفتار با services.queries)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal('0.01')))
    return str(value)


def stock_expression(prefix='movements__'):
    """
    همان CASE واقعی ``RawMaterial.current_stock``، فقط در سطح queryset.

    فقط ``consumption`` کم می‌کند و بقیهٔ movement_typeها اضافه می‌کنند.
    """
    return Sum(
        Case(
            When(**{f'{prefix}movement_type': 'consumption'},
                 then=-F(f'{prefix}quantity')),
            default=F(f'{prefix}quantity'),
            output_field=MONEY_FIELD,
        )
    )


def stock_rows(queryset=None):
    """موجودی واقعی هر ماده، با یک کوئری (بدون N+1 روی current_stock)."""
    qs = queryset if queryset is not None else RawMaterial.objects.all()
    return qs.annotate(
        computed_stock=Coalesce(stock_expression(), Value(ZERO, output_field=MONEY_FIELD))
    )


# ----------------------------------------------------------------------
# پوشش نگاشت BOM → RawMaterial
# ----------------------------------------------------------------------

def bom_mapping_coverage(order):
    """
    پوشش نگاشت BOM → RawMaterial برای تسک‌های باز یک سفارش.

    اگر حتی یک تسک باز غیرنقاشی به RawMaterial نرسد، نیاز مواد سفارش
    قابل محاسبه نیست و نباید «بدون کمبود» گزارش شود.
    """
    tasks = ProductionTask.objects.filter(order_id=order.id, status__in=OPEN_STATUSES)

    nonpaint = tasks.exclude(station_name='paint')
    nonpaint_total = nonpaint.count()
    nonpaint_resolved = nonpaint.filter(
        part__isnull=False, part__material__raw_material__isnull=False
    ).count()

    paint_total = tasks.filter(station_name='paint').count()
    paint_resolved = _paint_tasks_with_requirements(order)

    # فقط Materialهایی را بررسی می‌کنیم که واقعاً در قطعات تسک‌های باز این
    # سفارش مورد نیازند؛ Material نامرتبط در کل دیتابیس نباید سفارش را ناقص کند.
    required_material_ids = set(
        nonpaint
        .filter(part__isnull=False, part__material_id__isnull=False)
        .values_list('part__material_id', flat=True)
    )
    required_materials = Material.objects.filter(pk__in=required_material_ids)
    required_materials_total = required_materials.count()
    required_materials_unmapped = required_materials.filter(
        raw_material__isnull=True
    ).count()

    return {
        'nonpaint_tasks': nonpaint_total,
        'nonpaint_tasks_resolved': nonpaint_resolved,
        'nonpaint_tasks_unresolved': max(nonpaint_total - nonpaint_resolved, 0),
        'paint_tasks': paint_total,
        'paint_tasks_with_requirement': paint_resolved,
        'paint_tasks_without_requirement': max(paint_total - paint_resolved, 0),
        # این دو فیلد برای سازگاری گزارش/تست‌ها نگه داشته شده‌اند، اما اکنون
        # scoped به Materialهای واقعاً مورد نیاز همین سفارش هستند.
        'material_rows': required_materials_total,
        'material_rows_unmapped': required_materials_unmapped,
        'material_rows_scope': 'open_order_tasks',
        'mapping_complete': (
            required_materials_total > 0
            and required_materials_unmapped == 0
            and nonpaint_total == nonpaint_resolved
        ),
    }


def _paint_tasks_with_requirements(order):
    """
    تسک‌های نقاشیِ بازی که برایشان PaintingMaterialRequirement تعریف شده است.

    این یک پوشش *تقریبی* است (وجود ردیف requirement برای همان
    محصول/فرآیند/بخش رنگ) و همیشه DERIVED گزارش می‌شود؛ چون مسیر واقعی
    (``product.utils.get_painting_material_requirements_for_task``) واریانت‌های
    رنگ را هم در نظر می‌گیرد.
    """
    requirement_exists = PaintingMaterialRequirement.objects.filter(
        product_id=OuterRef('order_item__product_id'),
        process_id=OuterRef('painting_stage__process_id'),
        color_part=OuterRef('color_part'),
    )
    return (
        ProductionTask.objects
        .filter(
            order_id=order.id,
            status__in=OPEN_STATUSES,
            station_name='paint',
            painting_stage__isnull=False,
            order_item__isnull=False,
        )
        .annotate(_has_requirement=Exists(requirement_exists))
        .filter(_has_requirement=True)
        .count()
    )


# ----------------------------------------------------------------------
# تأثیر انبار روی سفارش‌های باز
# ----------------------------------------------------------------------

def material_impact(raw_material_id=None, order_id=None, limit=25):
    """
    تحلیل تأثیر مواد — دامنهٔ انبار به سفارش.

    ``raw_material_id`` → همان ماده؛ ``order_id`` → نمای مواد یک سفارش از
    سمت درخواست‌های انبار (نه BOM، چون نگاشت BOM در داده وجود ندارد).
    """
    limit = max(1, min(int(limit or 25), 100))

    if order_id is not None:
        return order_material_impact(order_id)
    return warehouse_impact(raw_material_id=raw_material_id, limit=limit)


def warehouse_impact(raw_material_id=None, limit=25):
    """موجودی انبار → درخواست‌ها/مصرف → سفارش‌های درگیر."""
    qs = RawMaterial.objects.all()
    if raw_material_id is not None:
        qs = qs.filter(pk=raw_material_id)
        exists = qs.exists()
        if not exists:
            return None

    rows = list(
        stock_rows(qs)
        .select_related('category')
        .order_by('category__name', 'name')[:limit]
    )

    raw_ids = [row.id for row in rows]
    open_issues = _open_issue_index(raw_ids)
    consumption = _consumption_index(raw_ids)

    results = []
    for row in rows:
        issue_map = open_issues.get(row.id, {})
        use_map = consumption.get(row.id, {})
        stock = row.computed_stock or ZERO

        limitations = []
        signals = [SIGNAL_REAL]
        if row.min_stock_alert is not None and stock <= row.min_stock_alert:
            signals.append(SIGNAL_DERIVED)

        results.append({
            'raw_material_id': row.id,
            'raw_material': row.name,
            'category': row.category.name if row.category_id else None,
            'unit': row.get_unit_display(),
            'current_stock': money(stock),
            'min_stock_alert': money(row.min_stock_alert),
            'pack_size': money(row.pack_size),
            'stock_state': _stock_state(stock, row.min_stock_alert),
            'open_issue_count': issue_map.get('count', 0),
            'requested_quantity': money(issue_map.get('requested', ZERO)),
            'issued_quantity': money(issue_map.get('issued', ZERO)),
            'remaining_quantity': money(issue_map.get('remaining', ZERO)),
            'affected_open_orders': issue_map.get('order_count', 0),
            'affected_open_order_ids': issue_map.get('order_ids', [])[:MAX_ORDER_IDS],
            'affected_orders_truncated': (
                issue_map.get('order_count', 0) > MAX_ORDER_IDS
            ),
            'consumption_movements': use_map.get('count', 0),
            'consumption_quantity': money(use_map.get('quantity', ZERO)),
            'consuming_orders': use_map.get('order_count', 0),
            'limitations': limitations,
            'limitations_detail': list(limitation_messages(limitations)),
            'confidence': confidence_from_signals(signals),
            'evidence': [
                evidence.to_dict() for evidence in (
                    Evidence(
                        metric='current_stock',
                        value=money(stock),
                        unit=row.get_unit_display(),
                        source='inventory.RawMaterial.current_stock',
                        query=(
                            'sum of stock movements where only movement_type=consumption '
                            'subtracts (purchase, adjustment, return add)'
                        ),
                        threshold=f'min_stock_alert = {money(row.min_stock_alert)}',
                    ),
                    Evidence(
                        metric='pending_queue_rows',
                        value=issue_map.get('count', 0),
                        unit='queue row',
                        source='inventory.DailyMaterialQueue',
                        query=(
                            f"pending daily queue rows for raw_material_id = {row.id} "
                            "where status = 'pending'"
                        ),
                    ),
                    Evidence(
                        metric='affected_open_orders',
                        value=issue_map.get('order_count', 0),
                        unit='order',
                        source='inventory.DailyMaterialQueue via task/order item/defect',
                        query=(
                            'distinct orders referenced by pending daily queue rows '
                            '(source task, order item or rework defect)'
                        ),
                    ),
                )
            ],
        })

    # تحلیل انبار دادهٔ واقعی دارد؛ فقط مسیر BOM برای سفارش‌ها ناقص است.
    limitations = []
    if Material.objects.filter(raw_material__isnull=True).exists():
        limitations.append('missing_material_mapping')

    evidence = [Evidence(
        metric='material_rows_considered',
        value=len(results),
        unit='raw_material',
        source='inventory.RawMaterial',
        query='raw materials with computed stock, pending queue rows and consumption link',
    )]

    report_status = 'insufficient_data' if not results else 'complete'
    if report_status == 'insufficient_data':
        confidence = CONFIDENCE_LOW
        confidence_reasons = ['هیچ مادهٔ اولیه‌ای برای تحلیل پیدا نشد.']
    elif not rows or rows[0].computed_stock is None:
        confidence = CONFIDENCE_MEDIUM
        confidence_reasons = ['موجودی از گردش انبار محاسبه شده است.']
    else:
        confidence = CONFIDENCE_HIGH
        confidence_reasons = [
            'موجودی، درخواست‌ها و مصرف مستقیماً از دیتابیس آمده‌اند.'
        ]

    return {
        'report_status': report_status,
        'scope': 'warehouse',
        'raw_material_id': raw_material_id,
        'materials': results,
        'materials_returned': len(results),
        'materials_total': RawMaterial.objects.count(),
        'truncated': RawMaterial.objects.count() > len(results),
        'order_scoped_bom': unavailable('missing_material_mapping').to_dict(),
        'confidence': confidence,
        'confidence_reasons': confidence_reasons,
        'limitations': limitations,
        'limitations_detail': list(limitation_messages(limitations)),
        'evidence': [e.to_dict() for e in evidence],
    }


def _open_issue_index(raw_ids):
    """
    اندیس ردیف‌های «در انتظار تحویل» صف روزانه به تفکیک ماده.

    Engine B به‌جای «درخواست مواد» یک ردیف صف دارد، پس این اندیس همان شمارش را
    از جدول ``DailyMaterialQueue`` می‌سازد: نیاز برنامه‌ریزی‌شده در برابر مصرف
    واقعی، به تفکیک ماده و سفارش.
    """
    index = {}
    if not raw_ids:
        return index

    rows = (
        DailyMaterialQueue.objects
        .filter(raw_material_id__in=raw_ids, status='pending')
        .values(
            'raw_material_id',
            'sources__production_task__order_id',
            'sources__production_task__order_item__order_id',
            'sources__defect__order_id',
            'planned_quantity', 'delivered_quantity', 'returned_quantity',
        )
    )
    for row in rows:
        entry = index.setdefault(row['raw_material_id'], {
            'count': 0, 'requested': ZERO, 'issued': ZERO, 'remaining': ZERO,
            'order_ids': set(),
        })
        entry['count'] += 1
        entry['requested'] += row['planned_quantity'] or ZERO
        entry['issued'] += row['delivered_quantity'] or ZERO
        entry['remaining'] += (row['planned_quantity'] or ZERO) - (row['delivered_quantity'] or ZERO)
        entry['remaining'] = max(ZERO, entry['remaining'])
        order_id = (
            row['sources__production_task__order_id']
            or row['sources__production_task__order_item__order_id']
            or row['sources__defect__order_id']
        )
        if order_id:
            entry['order_ids'].add(order_id)

    for entry in index.values():
        entry['order_ids'] = sorted(entry['order_ids'])
        entry['order_count'] = len(entry['order_ids'])
    return index


def _consumption_index(raw_ids):
    """اندیس مصرف واقعی به تفکیک ماده — یک کوئری گروه‌بندی‌شده."""
    index = {}
    if not raw_ids:
        return index
    rows = (
        StockMovement.objects
        .filter(raw_material_id__in=raw_ids, movement_type='consumption')
        .values('raw_material_id')
        .annotate(
            count=Count('id'),
            quantity=Sum('quantity'),
            # هر movement یک «مالک سفارش» دارد: reference_task اولویت دارد و
            # برای مصرف‌های نقاشی reference_order_item استفاده می‌شود.
            # Case داخل Count باعث می‌شود کل محاسبه همچنان یک query بماند.
            order_count=Count(
                Case(
                    When(
                        reference_task__order_id__isnull=False,
                        then=F('reference_task__order_id'),
                    ),
                    default=F('reference_order_item__order_id'),
                    output_field=IntegerField(),
                ),
                distinct=True,
            ),
        )
    )
    for row in rows:
        index[row['raw_material_id']] = {
            'count': row['count'] or 0,
            'quantity': row['quantity'] or ZERO,
            'order_count': row['order_count'] or 0,
        }
    return index


def _stock_state(stock, min_stock_alert):
    """همان قاعدهٔ واقعی CraftFlow: stock <= min_stock_alert یعنی کم‌موجود."""
    if stock is None:
        stock = ZERO
    threshold = min_stock_alert or ZERO
    if stock <= 0:
        return 'out_of_stock'
    if stock <= threshold:
        return 'low'
    return 'ok'


# ----------------------------------------------------------------------
# نمای مواد یک سفارش (فقط از سمت درخواست‌های انبار)
# ----------------------------------------------------------------------

def order_material_impact(order_id):
    """
    مواد یک سفارش از منظر انبار.

    اگر نگاشت BOM وجود نداشته باشد، نتیجه ``insufficient_data`` است و
    **هرگز** «بدون کمبود» گزارش نمی‌شود.
    """
    try:
        order = Order.objects.get(pk=order_id)
    except (Order.DoesNotExist, ValueError, TypeError):
        return None

    coverage = bom_mapping_coverage(order)
    pending_queues = list(
        DailyMaterialQueue.objects
        .filter(
            Q(sources__production_task__order_id=order.id)
            | Q(sources__production_task__order_item__order_id=order.id)
            | Q(sources__defect__order_id=order.id)
        )
        .exclude(status='cancelled')
        .distinct()
        .select_related('raw_material')
        .order_by('raw_material_id', 'id')
    )
    open_queues = [q for q in pending_queues if q.status == 'pending']

    shortage_rows, plan_logic = _shortages_for(open_queues)

    limitations = []
    if not coverage['mapping_complete']:
        limitations.append('missing_material_mapping')
    if coverage['paint_tasks_without_requirement']:
        limitations.append('partial_paint_material_coverage')

    if limitations:
        confidence = CONFIDENCE_LOW
        confidence_reasons = [
            'منبع تصمیم «product.Material.raw_material» در دادهٔ فعلی وجود ندارد.'
        ]
    else:
        confidence = CONFIDENCE_MEDIUM if open_queues else CONFIDENCE_HIGH
        confidence_reasons = [
            'صف مواد روزانه و موجودی مستقیماً از دیتابیس آمده‌اند.'
        ]

    findings = []
    if shortage_rows:
        findings.append(Finding(
            finding_id=f'order:{order.id}:material_shortage',
            domain='materials',
            severity='high',
            summary=(
                f'{len(shortage_rows)} ماده برای سفارش {order.id} کمبود فیزیکی دارد.'
            ),
            evidence=(Evidence(
                metric='shortage',
                value=len(shortage_rows),
                unit='raw_material',
                source='inventory.services.build_plans',
                query='pending queue rows of this order evaluated with the real delivery rule',
            ),),
            confidence=confidence,
            limitations=tuple(limitations),
            derived=True,
        ))

    return {
        'order_id': order.id,
        'order_number': order.number or None,
        'report_status': 'insufficient_data' if limitations else 'complete',
        'status': 'insufficient_data' if limitations else 'healthy',
        'reason': 'missing_material_mapping' if not coverage['mapping_complete'] else None,
        'open_issue_count': len(open_queues),
        'total_issue_count': len(pending_queues),
        'shortages': shortage_rows,
        'shortage_count': len(shortage_rows),
        'plan_logic': plan_logic,
        'bom_requirements': unavailable('missing_material_mapping').to_dict(),
        'coverage': coverage,
        'limitations': limitations,
        'limitations_detail': list(limitation_messages(limitations)),
        'confidence': confidence,
        'confidence_reasons': confidence_reasons,
        'evidence': [Evidence(
            metric='open_queue_rows',
            value=len(open_queues),
            unit='queue row',
            source='inventory.DailyMaterialQueue',
            query=(
                'daily queue rows of this order via source task/order item/defect '
                "where status = 'pending'"
            ),
        ).to_dict(), Evidence(
            metric='material_rows_unmapped',
            value=coverage['material_rows_unmapped'],
            unit='row',
            source='product.Material.raw_material',
            query='material rows where raw_material_id is NULL',
        ).to_dict()],
        'findings': [f.to_dict() for f in findings],
    }


def _shortages_for(pending_queues):
    """
    کمبود با همان ریاضیات تحویل واقعی: ``inventory.services._physical_for``.

    هیچ ریاضیات جدیدی اختراع نمی‌شود؛ فقط گِرد کردن نیاز به بستهٔ کامل و مقایسه
    با موجودی فعلی، دقیقاً همان کاری که انباردار هنگام تحویل انجام می‌دهد.
    """
    from inventory.services import _physical_for

    logic = 'inventory.services._physical_for'
    if not pending_queues:
        return [], logic

    per_material = {}
    for queue in pending_queues:
        bucket = per_material.setdefault(queue.raw_material_id, {
            'raw': queue.raw_material, 'need': ZERO, 'queue_ids': [],
        })
        bucket['need'] += queue.planned_quantity or ZERO
        bucket['queue_ids'].append(queue.id)

    # Leftover (return StockMovements) per material — Engine B: returns add to
    # current_stock, so we separate them from base (purchase) stock for reporting.
    leftover_map = {}
    if per_material:
        leftover_map = dict(
            StockMovement.objects
            .filter(raw_material_id__in=per_material.keys(), movement_type='return')
            .values_list('raw_material_id')
            .annotate(total=Sum('quantity'))
        )
        leftover_map = {k: (v or ZERO) for k, v in leftover_map.items()}

    short = []
    for raw_id, bucket in per_material.items():
        raw = bucket['raw']
        need = bucket['need']
        if need <= 0:
            continue
        packs, physical = _physical_for(need, raw.pack_size or 0)
        leftover = leftover_map.get(raw_id, ZERO)
        base_stock = (raw.current_stock or ZERO) - leftover
        cover = base_stock + leftover
        if physical <= cover:
            continue
        from_leftover = min(leftover, physical)
        from_stock = min(base_stock, max(ZERO, physical - from_leftover))
        short.append({
            'raw_material_id': raw_id,
            'raw': raw,
            'need': need,
            'packs': packs,
            'pack_size': raw.pack_size or ZERO,
            'physical': physical,
            'stock': base_stock,
            'leftover': leftover,
            'from_leftover': from_leftover,
            'from_stock': from_stock,
            'queue_ids': bucket['queue_ids'],
        })

    short.sort(key=lambda p: (p['physical'] - p['stock'] - p['leftover']))

    return [{
        'raw_material_id': p['raw_material_id'],
        'raw_material': p['raw'].name,
        'needed': money(p['need']),
        'from_leftover': money(p['from_leftover']),
        'from_stock': money(p['from_stock']),
        'physical_required': money(p['physical']),
        'packs': p['packs'],
        'pack_size': money(p['pack_size']),
        'current_stock': money(p['stock'] + p['leftover']),
        'shortage_amount': money(p['physical'] - p['stock'] - p['leftover']),
        'issue_ids': list(p['queue_ids']),
    } for p in short], logic


__all__ = [
    'OPEN_DEFECT_STATUSES',
    'OPEN_ISSUE_STATUSES',
    'bom_mapping_coverage',
    'material_impact',
    'money',
    'order_material_impact',
    'stock_expression',
    'stock_rows',
    'warehouse_impact',
]