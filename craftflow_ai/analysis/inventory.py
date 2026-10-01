"""
تحلیل مواد (فاز ۲) — تأثیر انبار روی سفارش‌های باز.

مسیری که *واقعاً* در دادهٔ فعلی CraftFlow وجود دارد:

    RawMaterial → current_stock → MaterialIssue / StockMovement → سفارش‌های درگیر

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
    MaterialIssue,
    MaterialLeftover,
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

OPEN_ISSUE_STATUSES = ('requested', 'partial')
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

    materials_total = Material.objects.count()
    materials_unmapped = Material.objects.filter(raw_material__isnull=True).count()

    return {
        'nonpaint_tasks': nonpaint_total,
        'nonpaint_tasks_resolved': nonpaint_resolved,
        'nonpaint_tasks_unresolved': max(nonpaint_total - nonpaint_resolved, 0),
        'paint_tasks': paint_total,
        'paint_tasks_with_requirement': paint_resolved,
        'paint_tasks_without_requirement': max(paint_total - paint_resolved, 0),
        'material_rows': materials_total,
        'material_rows_unmapped': materials_unmapped,
        'mapping_complete': (
            materials_total > 0
            and materials_unmapped == 0
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
                        metric='open_issue_count',
                        value=issue_map.get('count', 0),
                        unit='request',
                        source='inventory.MaterialIssue',
                        query=(
                            f"open material issues for raw_material_id = {row.id} "
                            f"where status in {list(OPEN_ISSUE_STATUSES)}"
                        ),
                    ),
                    Evidence(
                        metric='affected_open_orders',
                        value=issue_map.get('order_count', 0),
                        unit='order',
                        source='inventory.MaterialIssue via task/order_item',
                        query=(
                            'distinct orders referenced by open material issues '
                            '(task.order_id or order_item.order_id)'
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
        query='raw materials with computed stock, open issues and consumption link',
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
    """اندیس درخواست‌های باز به تفکیک ماده — یک کوئری، گروه‌بندی در حافظه."""
    index = {}
    if not raw_ids:
        return index

    rows = (
        MaterialIssue.objects
        .filter(raw_material_id__in=raw_ids, status__in=OPEN_ISSUE_STATUSES)
        .values('raw_material_id', 'task__order_id', 'order_item__order_id',
                'requested_quantity', 'issued_quantity')
    )
    for row in rows:
        entry = index.setdefault(row['raw_material_id'], {
            'count': 0, 'requested': ZERO, 'issued': ZERO, 'remaining': ZERO,
            'order_ids': set(),
        })
        requested = row['requested_quantity'] or ZERO
        issued = row['issued_quantity'] or ZERO
        entry['count'] += 1
        entry['requested'] += requested
        entry['issued'] += issued
        entry['remaining'] += (requested - issued)
        order_id = row['task__order_id'] or row['order_item__order_id']
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
            order_count=Count('reference_task__order_id', distinct=True),
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
    issues = list(
        MaterialIssue.objects
        .filter(Q(task__order_id=order.id) | Q(order_item__order_id=order.id))
        .exclude(status='cancelled')
        .select_related('raw_material')
        .order_by('raw_material_id', 'id')
    )
    open_issues = [i for i in issues if i.status in OPEN_ISSUE_STATUSES]

    shortage_rows, plan_logic = _shortages_for(open_issues)

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
        confidence = CONFIDENCE_MEDIUM if open_issues else CONFIDENCE_HIGH
        confidence_reasons = [
            'درخواست‌های انبار و موجودی مستقیماً از دیتابیس آمده‌اند.'
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
                query='open issues of this order evaluated with the real handover plan',
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
        'open_issue_count': len(open_issues),
        'total_issue_count': len(issues),
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
            metric='open_material_issues',
            value=len(open_issues),
            unit='request',
            source='inventory.MaterialIssue',
            query=(
                'material issues of this order via task.order_id or order_item.order_id '
                f"where status in {list(OPEN_ISSUE_STATUSES)}"
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


def _shortages_for(open_issues):
    """
    کمبود با همان ریاضیات ``inventory.services.build_plans``.

    هیچ ریاضیات جدیدی اختراع نمی‌شود: باقی‌ماندهٔ سالن + گِرد کردن به بسته.
    """
    from inventory.services import build_plans

    if not open_issues:
        return [], 'inventory.services.build_plans'

    issues_qty = []
    for issue in open_issues:
        remaining = (issue.requested_quantity or ZERO) - (issue.issued_quantity or ZERO)
        if remaining > 0:
            issues_qty.append((issue, remaining))
    if not issues_qty:
        return [], 'inventory.services.build_plans'

    raw_ids = {issue.raw_material_id for issue, _ in issues_qty}
    leftovers = {
        row.raw_material_id: row.quantity
        for row in MaterialLeftover.objects.filter(raw_material_id__in=raw_ids)
    }
    plans = build_plans(issues_qty, leftovers)
    short = [p for p in plans if not p['enough']]
    short.sort(key=lambda p: (p['physical'] - p['stock']))

    return [{
        'raw_material_id': p['raw_material_id'],
        'raw_material': p['raw'].name,
        'needed': money(p['need']),
        'from_leftover': money(p['from_leftover']),
        'from_stock': money(p['from_stock']),
        'physical_required': money(p['physical']),
        'packs': p['packs'],
        'pack_size': money(p['pack_size']),
        'current_stock': money(p['stock']),
        'shortage_amount': money(p['physical'] - p['stock']),
        'issue_ids': list(p['issue_ids']),
    } for p in short], 'inventory.services.build_plans'


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