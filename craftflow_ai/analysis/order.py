"""
تحلیل سفارش (فاز ۲) — سلامت و تأخیر.

هر شش دامنهٔ سلامت (production / materials / painting / quality / packaging /
shipping) وضعیت صریح خودش را دارد؛ هیچ دامنه‌ای «به‌طور پیش‌فرض» سالم یا صفر
فرض نمی‌شود.

دو قاعدهٔ سخت این فایل:

1. **تاریخ سررسید ساخته نمی‌شود.** اگر ``Order.due_date`` خالی باشد، تأخیر
   فقط با قاعدهٔ خود CraftFlow («created_at + ۳ روز») و با ``confidence=low``
   گزارش می‌شود.
2. **گراف وابستگی وجود ندارد.** ``ProductionTask`` هیچ ``depends_on`` ندارد؛
   فقط قرارداد مشتق‌شدهٔ ``(order, part, step_order - 1)`` و برای نقاشی قرارداد
   ``(order, paint, order_item, color_part, step_order - 1)`` استفاده می‌شود که
   همیشه با برچسب ``DERIVED`` و محدودیت ``missing_dependency_graph`` گزارش می‌شود.
"""
import jdatetime
from django.db.models import Count, Q
from django.utils import timezone

from craftflow_ai.analysis.evidence import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SIGNAL_DERIVED,
    SIGNAL_MISSING,
    SIGNAL_REAL,
    DomainResult,
    Evidence,
    Finding,
    confidence_from_signals,
    rollup_status,
    worst_confidence,
)
from craftflow_ai.analysis.inventory import (
    OPEN_DEFECT_STATUSES,
    bom_mapping_coverage,
    order_material_impact,
)
from craftflow_ai.analysis.limits import (
    STATUS_UNKNOWN,
    limitation_messages,
    unavailable,
)
from craftflow_ai.analysis.production import (
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_WAITING,
    STATION_CODES,
    unknown_station_bucket,
)

from product.models import (
    Order,
    PackagingUnit,
    ProductionDefect,
    ProductionTask,
)

# همان قاعدهٔ خود CraftFlow در product.views.delayed_orders
DELAYED_AFTER_DAYS = 3
STALLED_AFTER_DAYS = 7

STAGE_PAINT = 'paint'

HEALTH_DOMAINS = (
    'production', 'materials', 'painting', 'quality', 'packaging', 'shipping',
)

# ----------------------------------------------------------------------
# رجیستری قطعی علت‌های تأخیر و شدت آن‌ها
# ----------------------------------------------------------------------

DELAY_CAUSE_PRECEDENCE = (
    'missing_schedule',
    'blocked_material',
    'blocked_quality',
    'awaiting_predecessor',
    'queue_at_station',
    'paint_not_started',
    'scheduled_overrun',
    'no_task_breakdown',
    'packaging_pending',
    'shipping_pending',
    'age_only',
)

DELAY_CAUSE_SEVERITY = {
    'blocked_material': 'high',
    'blocked_quality': 'high',
    'awaiting_predecessor': 'high',
    'queue_at_station': 'medium',
    'missing_schedule': 'medium',
    'scheduled_overrun': 'medium',
    'paint_not_started': 'medium',
    'age_only': 'low',
    'no_task_breakdown': 'low',
    'packaging_pending': 'low',
    'shipping_pending': 'low',
}

DELAY_CAUSE_LABELS = {
    'missing_schedule': 'بدون زمان‌بندی',
    'blocked_material': 'مسدود در مواد',
    'blocked_quality': 'مسدود در کیفیت',
    'awaiting_predecessor': 'در انتظار مرحلهٔ قبل',
    'queue_at_station': 'صف ایستگاه',
    'paint_not_started': 'نقاشی شروع نشده',
    'scheduled_overrun': 'عقب‌افتاده از زمان‌بندی',
    'no_task_breakdown': 'بدون شکست تسک',
    'packaging_pending': 'بسته‌بندی انجام نشده',
    'shipping_pending': 'ارسال انجام نشده',
    'age_only': 'فقط سن سفارش',
}


# ----------------------------------------------------------------------
# کمکی‌های مشترک
# ----------------------------------------------------------------------

def order_age_days(order):
    """سن سفارش به روز؛ ``created_at`` یک PersianDateField است."""
    if not getattr(order, 'created_at', None):
        return None
    try:
        return max((jdatetime.date.today() - order.created_at).days, 0)
    except TypeError:
        return None


def task_rows(order_id):
    """همهٔ تسک‌های یک سفارش در یک کوئری، به شکل دیکشنری ساده."""
    return list(
        ProductionTask.objects
        .filter(order_id=order_id)
        .order_by('step_order', 'id')
        .values(
            'id', 'part_id', 'order_item_id', 'station_name', 'step_order',
            'status', 'quantity', 'completed_quantity', 'color_part',
            'scheduled_start', 'scheduled_end', 'assigned_worker_id',
            'painting_stage_id',
        )
    )


def task_rollup(order_id, now=None):
    """
    تجمیع تسک‌های یک سفارش — یک کوئری، بدون N+1.

    ``overdue_pending`` مشتق‌شده است: pending با ``scheduled_end`` گذشته.
    """
    now = now or timezone.now()
    return ProductionTask.objects.filter(order_id=order_id).aggregate(
        total=Count('id'),
        done=Count('id', filter=Q(status=STATUS_DONE)),
        pending=Count('id', filter=Q(status=STATUS_PENDING)),
        waiting=Count('id', filter=Q(status=STATUS_WAITING)),
        overdue_pending=Count(
            'id', filter=Q(status=STATUS_PENDING, scheduled_end__lt=now)
        ),
        scheduled=Count('id', filter=Q(scheduled_start__isnull=False)),
        unscheduled_open=Count(
            'id', filter=Q(status__in=(STATUS_PENDING, STATUS_WAITING),
                           scheduled_start__isnull=True)
        ),
        unscheduled_nonpaint_open=Count(
            'id', filter=Q(
                status__in=(STATUS_PENDING, STATUS_WAITING),
                scheduled_start__isnull=True,
            ) & ~Q(station_name=STAGE_PAINT)
        ),
        unknown_station=Count('id', exclude=Q(station_name__in=STATION_CODES)),
        paint_tasks=Count('id', filter=Q(station_name=STAGE_PAINT)),
    )


def predecessor_key(task):
    """
    کلید تسک پیشین — قرارداد مشتق‌شدهٔ CraftFlow، نه گراف وابستگی.

    برای تسک‌های عادی: ``(order, part, step_order - 1)``
    برای نقاشی: ``(order, 'paint', order_item, color_part, step_order - 1)``
    """
    if task['station_name'] == STAGE_PAINT and task['order_item_id']:
        return (
            'paint', task['order_item_id'], task['color_part'],
            (task['step_order'] or 0) - 1,
        )
    return ('std', task['part_id'], (task['step_order'] or 0) - 1)


def successor_index(order_id):
    """اندیس تسک‌ها به تفکیک predecessor_key؛ برای تشخیص انتظار مرحلهٔ قبل."""
    rows = task_rows(order_id)
    return rows, {predecessor_key(row): row for row in rows}


def awaiting_predecessor_tasks(rows):
    """تسک‌های waiting که پیشینِ آن‌ها هنوز done نیست (DERIVED)."""
    index = {predecessor_key(row): row for row in rows}
    awaiting = []
    for row in rows:
        if row['status'] != STATUS_WAITING:
            continue
        previous = index.get(predecessor_key(row))
        if previous is None or previous['status'] != STATUS_DONE:
            awaiting.append({'task': row, 'predecessor': previous})
    return awaiting


# ----------------------------------------------------------------------
# سلامت سفارش
# ----------------------------------------------------------------------

def order_health(order_id):
    """
    سلامت شش‌دامنه‌ای یک سفارش.

    خروجی قطعی است؛ مدل زبانی فقط آن را توضیح می‌دهد.
    """
    try:
        order = Order.objects.select_related('customer').get(pk=order_id)
    except (Order.DoesNotExist, ValueError, TypeError):
        return None

    now = timezone.now()
    tasks = task_rollup(order.id, now=now)
    age_days = order_age_days(order)

    domains = [
        _production_domain(order, tasks, age_days, now),
        _materials_domain(order),
        _painting_domain(order, tasks),
        _quality_domain(order),
        _packaging_domain(order, tasks),
        _shipping_domain(order, tasks),
    ]

    statuses = [d.status for d in domains]
    unknown_domains = [d.domain for d in domains
                       if d.status in (STATUS_UNKNOWN, 'insufficient_data')]

    overall_confidence = worst_confidence(*[d.confidence for d in domains])
    findings = [f for d in domains for f in d.findings]
    limitation_codes = []
    for domain in domains:
        limitation_codes.extend(domain.limitations)

    overall_status = rollup_status(statuses)

    return {
        'order_id': order.id,
        'order_number': order.number or None,
        'customer': order.customer.name if order.customer_id else None,
        'order_status': order.status,
        'order_status_derived': (
            'Order.status از شمارش تسک‌ها مشتق می‌شود '
            '(ProductionTask.update_order_status) و state machine مستقل نیست.'
        ),
        'priority': order.priority,
        'due_date': order.due_date.isoformat() if order.due_date else None,
        'due_date_available': order.due_date is not None,
        'created_at': order.created_at.isoformat() if order.created_at else None,
        'age_days': age_days,
        'domains': {d.domain: d.to_dict() for d in domains},
        'domain_statuses': {d.domain: d.status for d in domains},
        'unknown_domains': unknown_domains,
        'unknown_station': unknown_station_bucket(),
        'status': overall_status,
        'confidence': overall_confidence,
        'confidence_reasons': _health_confidence_reasons(domains, overall_confidence),
        'findings': [f.to_dict() for f in findings],
        'limitations': list(dict.fromkeys(limitation_codes)),
        'limitations_detail': list(limitation_messages(limitation_codes)),
    }


def _health_confidence_reasons(domains, confidence):
    reasons = []
    for domain in domains:
        if domain.confidence != CONFIDENCE_HIGH and domain.confidence_reasons:
            reasons.append(f'{domain.domain}: ' + ' '.join(domain.confidence_reasons))
    if not reasons:
        return ['همهٔ دامنه‌ها بر پایهٔ دادهٔ واقعی و کافی محاسبه شدند.']
    return reasons


def _production_domain(order, tasks, age_days, now):
    total = tasks['total'] or 0
    done = tasks['done'] or 0
    pending = tasks['pending'] or 0
    waiting = tasks['waiting'] or 0
    overdue = tasks['overdue_pending'] or 0
    unknown_station = tasks['unknown_station'] or 0

    evidence = [
        Evidence(
            metric='tasks_total', value=total, unit='task',
            source='product.ProductionTask',
            query='tasks of this order grouped by status',
        ),
        Evidence(
            metric='tasks_done', value=done, unit='task',
            source='product.ProductionTask',
            query="tasks of this order where status = 'done'",
        ),
        Evidence(
            metric='tasks_pending', value=pending, unit='task',
            source='product.ProductionTask',
            query="tasks of this order where status = 'pending'",
        ),
        Evidence(
            metric='tasks_waiting', value=waiting, unit='task',
            source='product.ProductionTask',
            query="tasks of this order where status = 'waiting'",
        ),
        Evidence(
            metric='overdue_pending', value=overdue, unit='task',
            source='product.ProductionTask.scheduled_end',
            query=(
                'pending tasks of this order where scheduled_end is before now '
                '(derived; the model has no overdue status)'
            ),
            derived=True,
        ),
    ]

    limitations = ['missing_dependency_graph', 'missing_task_timestamp']
    if unknown_station:
        limitations.append('unknown_station_codes')
    if tasks['unscheduled_nonpaint_open'] or 0:
        limitations.append('missing_nonpaint_schedule')

    findings = []

    if total == 0:
        return DomainResult(
            domain='production',
            status='not_started',
            confidence=CONFIDENCE_HIGH,
            summary='این سفارش هیچ تسک تولیدی ندارد.',
            evidence=evidence,
            limitations=('missing_dependency_graph',),
            confidence_reasons=('Order.status نشان می‌دهد سفارش وارد تولید نشده است.',),
        )

    if done == total:
        status = 'complete'
        confidence = CONFIDENCE_HIGH
        summary = f'همهٔ {total} تسک این سفارش تکمیل شده‌اند.'
        signals = [SIGNAL_REAL]
    elif done > 0:
        signals = [SIGNAL_REAL]
        if overdue:
            status = 'warning'
            summary = (
                f'{done} تسک از {total} تکمیل شده و {overdue} تسک از زمان‌بندی '
                'خود عقب افتاده است.'
            )
            limitations = limitations + ('missing_nonpaint_schedule',)
            findings.append(Finding(
                finding_id=f'order:{order.id}:production_overdue',
                domain='production',
                severity='medium',
                summary=summary,
                evidence=(evidence[4],),
                confidence=CONFIDENCE_MEDIUM,
                limitations=('missing_nonpaint_schedule',),
                derived=True,
            ))
        else:
            status = 'healthy'
            summary = f'{done} تسک از {total} تکمیل شده و بقیه در جریان‌اند.'
    else:
        if pending:
            status = 'warning'
            summary = (
                f'هیچ تسکی تکمیل نشده، ولی {pending} تسک آمادهٔ انجام وجود دارد.'
            )
            signals = [SIGNAL_REAL]
        else:
            stalled = age_days is not None and age_days > STALLED_AFTER_DAYS
            status = 'stalled' if stalled else 'blocked'
            summary = (
                f'{waiting} تسک در انتظار مرحلهٔ قبل‌اند و هیچ تسکی آمادهٔ انجام نیست.'
            )
            if stalled:
                summary += (
                    f' سنجش توقف بر مبنای سن سفارش ({age_days} روز) است، '
                    'چون تسک فیلد زمان ایجاد ندارد.'
                )
            signals = [SIGNAL_REAL, SIGNAL_DERIVED]
            findings.append(Finding(
                finding_id=f'order:{order.id}:awaiting_predecessor',
                domain='production',
                severity='high' if stalled else 'medium',
                summary=summary,
                evidence=(evidence[3], Evidence(
                    metric='order_age_days', value=age_days, unit='day',
                    source='product.Order.created_at',
                    query='age of this order (proxy for task age)',
                    derived=True,
                )),
                confidence=CONFIDENCE_MEDIUM,
                limitations=('missing_dependency_graph', 'missing_task_timestamp'),
                derived=True,
            ))

    confidence = confidence_from_signals(signals)
    return DomainResult(
        domain='production',
        status=status,
        confidence=confidence,
        summary=summary,
        evidence=evidence,
        limitations=limitations,
        findings=findings,
        confidence_reasons=(
            'شمارش تسک‌ها مستقیماً از ProductionTask آمده است.' if total
            else 'داده‌ای برای تولید این سفارش وجود ندارد.'
        ),
        detail={
            'progress_percent': int(round(done * 100 / total)) if total else 0,
            'derived_fields': {
                'overdue_pending': 'pending + scheduled_end گذشته (مشتق‌شده)',
                'predecessor': '(order, part, step_order - 1) — قرارداد مشتق‌شده',
            },
        },
    )


def _materials_domain(order):
    impact = order_material_impact(order.id)
    evidence = [
        Evidence(
            metric='pending_queue_rows',
            value=impact['open_issue_count'],
            unit='queue row',
            source='inventory.DailyMaterialQueue',
            query=(
                'pending daily queue rows of this order via source task/order item/defect'
            ),
        ),
        Evidence(
            metric='material_shortages',
            value=impact['shortage_count'],
            unit='raw_material',
            source='inventory.services.build_plans',
            query='open issues evaluated with the real handover plan arithmetic',
        ),
        Evidence(
            metric='material_rows_unmapped',
            value=impact['coverage']['material_rows_unmapped'],
            unit='row',
            source='product.Material.raw_material',
            query='material rows where raw_material_id is NULL',
        ),
    ]

    if impact['limitations']:
        missing_bom = impact['coverage']['material_rows_unmapped'] > 0 or (
            impact['coverage']['nonpaint_tasks_unresolved'] > 0
        )
        code = 'missing_material_mapping' if missing_bom else (
            impact['limitations'][0]
        )
        return DomainResult(
            domain='materials',
            status='insufficient_data',
            confidence=CONFIDENCE_LOW,
            summary=(
                'نیاز مواد این سفارش قابل محاسبه نیست، چون مسیر '
                'BOM → RawMaterial در دادهٔ فعلی تکمیل نشده است.'
            ),
            evidence=evidence,
            limitations=tuple(impact['limitations']),
            findings=(),
            confidence_reasons=tuple(impact['confidence_reasons']),
            detail={
                'coverage': impact['coverage'],
                'open_issue_count': impact['open_issue_count'],
                'shortage_count': impact['shortage_count'],
                'bom_requirements': impact['bom_requirements'],
                'note': (
                    'نبودِ ردیف کمبود در این حالت «سالم بودن» نیست؛ نتیجه '
                    'insufficient_data است.'
                ),
            },
        )

    if impact['shortage_count']:
        status = 'warning'
        summary = (
            f'{impact["shortage_count"]} ماده برای این سفارش کمبود فیزیکی دارد.'
        )
    elif impact['open_issue_count']:
        status = 'in_progress'
        summary = (
            f'{impact["open_issue_count"]} درخواست مواد باز است و کمبودی دیده نشد.'
        )
    elif impact['total_issue_count']:
        status = 'complete'
        summary = 'همهٔ درخواست‌های مواد این سفارش تحویل یا لغو شده‌اند.'
    else:
        status = 'not_started'
        summary = 'هیچ درخواست موادی برای این سفارش ثبت نشده است.'

    return DomainResult(
        domain='materials',
        status=status,
        confidence=impact['confidence'],
        summary=summary,
        evidence=evidence,
        limitations=tuple(impact['limitations']),
        findings=(),
        confidence_reasons=tuple(impact['confidence_reasons']),
        detail={'coverage': impact['coverage']},
    )


def _painting_domain(order, tasks):
    items = order.items.count()
    paint_tasks = tasks['paint_tasks'] or 0
    paint_done = ProductionTask.objects.filter(
        order_id=order.id, station_name=STAGE_PAINT, status=STATUS_DONE
    ).count()
    paint_pending = ProductionTask.objects.filter(
        order_id=order.id, station_name=STAGE_PAINT, status=STATUS_PENDING
    ).count()
    paint_waiting = max(paint_tasks - paint_done - paint_pending, 0)

    # معادل is_painting_complete هر OrderItem، اما بدون N+1 روی property:
    # قلم «نقاشی‌تمام‌شده» یعنی یا اصلاً تسک نقاشی ندارد، یا همهٔ تسک‌های
    # نقاشی‌اش done هستند.
    items_with_paint = order.items.filter(
        paint_tasks__station_name=STAGE_PAINT
    ).values('id').distinct().count()
    items_fully_painted = items_with_paint - (
        order.items
        .filter(paint_tasks__station_name=STAGE_PAINT)
        .exclude(paint_tasks__status=STATUS_DONE)
        .values('id').distinct().count()
    )
    painting_complete_items = (items - items_with_paint) + items_fully_painted

    evidence = [
        Evidence(
            metric='paint_tasks', value=paint_tasks, unit='task',
            source='product.ProductionTask',
            query="tasks of this order where station_name = 'paint'",
        ),
        Evidence(
            metric='paint_tasks_done', value=paint_done, unit='task',
            source='product.ProductionTask',
            query="done paint tasks of this order",
        ),
        Evidence(
            metric='order_items', value=items, unit='order_item',
            source='product.OrderItem',
            query='order items of this order',
        ),
    ]

    limitations = ['missing_dependency_graph']
    if paint_tasks:
        limitations.append('partial_paint_material_coverage')

    if not items:
        status, summary = 'not_started', 'این سفارش هیچ قلمی ندارد.'
    elif not paint_tasks:
        status = 'complete'
        summary = 'این سفارش هیچ تسک نقاشی ندارد؛ نیازی به نقاشی نبوده است.'
        evidence.append(Evidence(
            metric='painting_required', value=False, unit='boolean',
            source='product.ProductionTask',
            query="tasks of this order where station_name = 'paint' (none found)",
        ))
    elif paint_done == paint_tasks:
        status = 'complete'
        summary = f'همهٔ {paint_tasks} تسک نقاشی تکمیل شده‌اند.'
    elif paint_pending:
        status = 'in_progress'
        summary = f'{paint_done} از {paint_tasks} تسک نقاشی تکمیل شده است.'
    else:
        status = 'blocked'
        summary = 'همهٔ تسک‌های نقاشی در انتظار مرحلهٔ قبل هستند.'
        findings = [Finding(
            finding_id=f'order:{order.id}:paint_waiting',
            domain='painting',
            severity='medium',
            summary=summary,
            evidence=(evidence[0],),
            confidence=CONFIDENCE_MEDIUM,
            limitations=('missing_dependency_graph',),
            derived=True,
        )]
        return DomainResult(
            domain='painting',
            status=status,
            confidence=CONFIDENCE_MEDIUM,
            summary=summary,
            evidence=evidence,
            limitations=limitations,
            findings=findings,
            confidence_reasons=(
                'شمارش تسک‌های نقاشی مستقیماً از ProductionTask آمده است.',
            ),
            detail={'paint_waiting': paint_waiting},
        )

    return DomainResult(
        domain='painting',
        status=status,
        confidence=CONFIDENCE_HIGH,
        summary=summary,
        evidence=evidence,
        limitations=limitations,
        findings=(),
        confidence_reasons=(
            'شمارش تسک‌های نقاشی مستقیماً از ProductionTask آمده است.',
        ),
        detail={
            'paint_pending': paint_pending,
            'paint_waiting': paint_waiting,
            'painting_complete_items': painting_complete_items,
            'order_items': items,
            'rule': (
                'معادل OrderItem.is_painting_complete، اما با شمارش تجمیعی '
                'به‌جای property روی هر قلم.'
            ),
        },
    )


def _quality_domain(order):
    total_all = ProductionDefect.objects.count()
    order_defects = ProductionDefect.objects.filter(order_id=order.id)
    order_total = order_defects.count()
    order_open = order_defects.exclude(status='closed').count()
    blocking = order_defects.filter(status__in=OPEN_DEFECT_STATUSES).count()

    evidence = [
        Evidence(
            metric='defects_for_order', value=order_total, unit='defect',
            source='product.ProductionDefect',
            query='defects of this order grouped by status',
        ),
        Evidence(
            metric='open_defects_for_order', value=order_open, unit='defect',
            source='product.ProductionDefect',
            query="defects of this order where status != 'closed'",
        ),
        Evidence(
            metric='defects_in_database', value=total_all, unit='defect',
            source='product.ProductionDefect',
            query='total rows in ProductionDefect (database-wide)',
        ),
    ]

    if total_all == 0:
        return DomainResult(
            domain='quality',
            status=STATUS_UNKNOWN,
            confidence=CONFIDENCE_LOW,
            summary=(
                'جدول ProductionDefect در کل دیتابیس خالی است؛ نبودِ خرابی '
                'قابل گزارش به‌عنوان «سالم» نیست.'
            ),
            evidence=evidence,
            limitations=('missing_quality_events',),
            findings=(),
            confidence_reasons=(
                'جدول رویداد کیفی خالی است؛ هیچ شاهدی برای قضاوت وجود ندارد.',
            ),
            detail={'database_wide_defects': 0},
        )

    if blocking:
        status = 'blocked'
        summary = f'{blocking} خرابیِ حل‌نشده برای این سفارش وجود دارد.'
        confidence = CONFIDENCE_MEDIUM
    elif order_open:
        status = 'warning'
        summary = f'{order_open} خرابی باز برای این سفارش وجود دارد.'
        confidence = CONFIDENCE_MEDIUM
    elif order_total:
        status = 'complete'
        summary = f'همهٔ {order_total} خرابی این سفارش بسته شده‌اند.'
        confidence = CONFIDENCE_HIGH
    else:
        status = 'healthy'
        summary = 'برای این سفارش خرابی ثبت نشده و جدول کیفی خالی نیست.'
        confidence = CONFIDENCE_HIGH

    return DomainResult(
        domain='quality',
        status=status,
        confidence=confidence,
        summary=summary,
        evidence=evidence,
        limitations=('missing_dependency_graph',),
        findings=(),
        confidence_reasons=(
            'رویدادهای کیفی این سفارش مستقیماً از ProductionDefect آمده‌اند.'
            if order_total else
            'جدول کیفی خالی نیست و خرابی‌ای برای این سفارش ثبت نشده است.'
        ),
        detail={'database_wide_defects': total_all},
    )


def _packaging_units(order_id):
    return PackagingUnit.objects.filter(order_item__order_id=order_id)


def _packaging_domain(order, tasks):
    units = _packaging_units(order.id)
    total = units.count()
    packed = units.filter(is_packed=True).count()
    shipped = units.filter(is_shipped=True).count()

    evidence = [
        Evidence(
            metric='packaging_units', value=total, unit='unit',
            source='product.PackagingUnit',
            query='packaging units of the order items of this order',
        ),
        Evidence(
            metric='packed_units', value=packed, unit='unit',
            source='product.PackagingUnit.is_packed',
            query='packaging units of this order where is_packed is true',
        ),
    ]

    production_done = (tasks['done'] or 0) and (
        (tasks['done'] or 0) == (tasks['total'] or 0)
    )

    if not total:
        status = 'not_started'
        summary = 'هیچ واحد بسته‌بندی برای این سفارش ساخته نشده است.'
        confidence = CONFIDENCE_HIGH
    elif packed == total:
        status = 'complete'
        summary = f'همهٔ {total} واحد بسته‌بندی شده‌اند.'
        confidence = CONFIDENCE_HIGH
    elif packed:
        status = 'in_progress'
        summary = f'{packed} از {total} واحد بسته‌بندی شده است.'
        confidence = CONFIDENCE_HIGH
    else:
        status = 'warning'
        summary = f'هیچ‌کدام از {total} واحد بسته‌بندی نشده است.'
        confidence = CONFIDENCE_MEDIUM

    findings = []
    if status == 'warning' and production_done:
        limitations = ('missing_task_timestamp',)
        findings.append(Finding(
            finding_id=f'order:{order.id}:packaging_pending',
            domain='packaging',
            severity='medium',
            summary='تولید کامل شده ولی بسته‌بندی آغاز نشده است.',
            evidence=tuple(evidence),
            confidence=CONFIDENCE_MEDIUM,
            limitations=limitations,
            derived=True,
        ))

    return DomainResult(
        domain='packaging',
        status=status,
        confidence=confidence,
        summary=summary,
        evidence=evidence,
        limitations=(),
        findings=findings,
        confidence_reasons=(
            'وضعیت بسته‌بندی مستقیماً از PackagingUnit.is_packed آمده است.',
        ),
        detail={'shipped_units': shipped},
    )


def _shipping_domain(order, tasks):
    units = _packaging_units(order.id)
    total = units.count()
    packed = units.filter(is_packed=True).count()
    shipped = units.filter(is_shipped=True).count()

    evidence = [
        Evidence(
            metric='packaging_units', value=total, unit='unit',
            source='product.PackagingUnit',
            query='packaging units of the order items of this order',
        ),
        Evidence(
            metric='shipped_units', value=shipped, unit='unit',
            source='product.PackagingUnit.is_shipped',
            query='packaging units of this order where is_shipped is true',
        ),
    ]

    if not total:
        status = 'not_started'
        summary = 'هیچ واحد بسته‌بندی برای ارسال وجود ندارد.'
        confidence = CONFIDENCE_HIGH
    elif shipped == total:
        status = 'complete'
        summary = f'همهٔ {total} واحد ارسال شده‌اند.'
        confidence = CONFIDENCE_HIGH
    elif shipped:
        status = 'in_progress'
        summary = f'{shipped} از {total} واحد ارسال شده است.'
        confidence = CONFIDENCE_HIGH
    else:
        status = 'warning'
        summary = f'هیچ واحدی ارسال نشده است ({packed} واحد بسته‌بندی‌شده).'
        confidence = CONFIDENCE_HIGH

    return DomainResult(
        domain='shipping',
        status=status,
        confidence=confidence,
        summary=summary,
        evidence=evidence,
        limitations=(),
        findings=(),
        confidence_reasons=(
            'وضعیت ارسال مستقیماً از PackagingUnit.is_shipped آمده است.',
        ),
        detail={'packed_units': packed},
    )


# ----------------------------------------------------------------------
# تأخیر سفارش
# ----------------------------------------------------------------------

def order_delay(order_id, days=None):
    """
    علت قطعی تأخیر یک سفارش با تقدم ثابت.

    علت‌ها به ترتیب زیر بررسی می‌شوند و **اولین** علتی که صدق کند انتخاب
    می‌شود؛ بنابراین نتیجه کاملاً قطعی است.
    """
    try:
        order = Order.objects.select_related('customer').get(pk=order_id)
    except (Order.DoesNotExist, ValueError, TypeError):
        return None

    days = DELAYED_AFTER_DAYS if days is None else max(0, int(days))
    now = timezone.now()
    age_days = order_age_days(order)

    rows = task_rollup(order.id, now=now)
    tasks, _index = successor_index(order.id)

    facts = _delay_facts(order, rows, tasks, age_days, days, now)

    # همان قاعدهٔ خود CraftFlow: سفارش تکمیل‌شده هرگز «عقب‌افتاده» نیست.
    if order.status == 'completed':
        return _not_delayed(order, facts, reason='order_status_completed')
    cause = _select_cause(facts)
    if cause is None:
        return _not_delayed(order, facts, reason='no_delay_cause_matched')

    severity = DELAY_CAUSE_SEVERITY.get(cause, 'low')
    merged_limitations = list(dict.fromkeys(
        list(facts['limitations']) + list(facts['cause_limitations'].get(cause, ()))
    ))
    finding = Finding(
        finding_id=f'order:{order.id}:delay:{cause}',
        domain='delay',
        severity=severity,
        summary=facts['cause_summaries'].get(cause, DELAY_CAUSE_LABELS.get(cause, cause)),
        evidence=tuple(facts['cause_evidence'].get(cause, ())),
        confidence=facts['confidence'],
        limitations=merged_limitations,
        derived=bool(facts['cause_limitations'].get(cause)),
    )

    return {
        'order_id': order.id,
        'order_number': order.number or None,
        'customer': order.customer.name if order.customer_id else None,
        'is_delayed': True,
        'severity': severity,
        'cause': facts['cause_summaries'].get(cause, DELAY_CAUSE_LABELS.get(cause)),
        'cause_code': cause,
        'cause_label': DELAY_CAUSE_LABELS.get(cause),
        'severity_reason': (
            f'قاعدهٔ قطعی شدت: «{DELAY_CAUSE_LABELS.get(cause, cause)}» در سطح '
            f'«{severity}» تعریف شده است.'
        ),
        'severity_registry': dict(DELAY_CAUSE_SEVERITY),
        'cause_precedence': list(DELAY_CAUSE_PRECEDENCE),
        'delay': facts['delay'],
        'age_days': age_days,
        'priority': order.priority,
        'due_date': order.due_date.isoformat() if order.due_date else None,
        'due_date_available': order.due_date is not None,
        'due_date_rule': (
            'Order.due_date خالی است؛ تأخیر فقط با قاعدهٔ سنی CraftFlow '
            f'(created_at + {DELAYED_AFTER_DAYS} روز) سنجیده می‌شود.'
        ) if order.due_date is None else 'Order.due_date موجود است.',
        'confidence': facts['confidence'],
        'confidence_reasons': facts['confidence_reasons'],
        'limitations': merged_limitations,
        'limitations_detail': list(limitation_messages(merged_limitations)),
        'evidence': facts['evidence'],
        'findings': [finding.to_dict()],
    }


def _not_delayed(order, facts, reason):
    """سفارش تأخیر ندارد؛ دلیل «چرا تأخیر نیست» هم صریح گزارش می‌شود."""
    return {
        'order_id': order.id,
        'order_number': order.number or None,
        'is_delayed': False,
        'severity': 'info',
        'cause': None,
        'cause_code': None,
        'cause_label': None,
        'no_delay_reason': reason,
        'severity_reason': 'هیچ‌کدام از علت‌های تأخیر صدق نکرد.',
        'delay': facts['delay'],
        'age_days': facts['age_days'],
        'priority': order.priority,
        'due_date': order.due_date.isoformat() if order.due_date else None,
        'due_date_available': order.due_date is not None,
        'due_date_rule': (
            'Order.due_date خالی است؛ تأخیر فقط با قاعدهٔ سنی CraftFlow '
            f'(created_at + {DELAYED_AFTER_DAYS} روز) سنجیده می‌شود.'
        ) if order.due_date is None else 'Order.due_date موجود است.',
        'confidence': facts['confidence'],
        'confidence_reasons': facts['confidence_reasons'],
        'limitations': facts['limitations'],
        'limitations_detail': list(limitation_messages(facts['limitations'])),
        'evidence': facts['evidence'],
        'findings': [],
        'severity_registry': dict(DELAY_CAUSE_SEVERITY),
        'cause_precedence': list(DELAY_CAUSE_PRECEDENCE),
    }


def _select_cause(facts):
    """اولین علتِ صدق‌کننده طبق تقدم ثابت — نه انتخاب بر اساس شدت."""
    for candidate in DELAY_CAUSE_PRECEDENCE:
        if facts['matched'].get(candidate):
            return candidate
    return None


def _delay_facts(order, rows, tasks, age_days, days, now):
    """جمع‌آوری همهٔ شواهد تأخیر و تعیین علت‌های منطبق."""
    total = rows['total'] or 0
    done = rows['done'] or 0
    pending = rows['pending'] or 0
    waiting = rows['waiting'] or 0
    unscheduled_open = rows['unscheduled_open'] or 0
    unscheduled_nonpaint_open = rows['unscheduled_nonpaint_open'] or 0
    overdue_pending = rows['overdue_pending'] or 0

    material_impact = order_material_impact(order.id)
    blocked_material = material_impact['open_issue_count']
    blocking_defects = ProductionDefect.objects.filter(
        order_id=order.id, status__in=OPEN_DEFECT_STATUSES
    ).count()

    awaiting = awaiting_predecessor_tasks(tasks)
    paint_rows = [t for t in tasks if t['station_name'] == STAGE_PAINT]
    paint_not_started = bool(paint_rows) and all(
        t['status'] == STATUS_WAITING for t in paint_rows
    )

    # ترتیب تقدم: missing_schedule فقط وقتی معنادار است که علت مسدودکننده‌ای
    # مثل مواد یا کیفیت وجود نداشته باشد. بدون این شرط، نبودِ زمان‌بندی همیشه
    # برنده می‌شد و علت واقعی را پنهان می‌کرد.
    blocking_material_or_quality = bool(blocked_material or blocking_defects)
    missing_schedule = bool(
        unscheduled_nonpaint_open
        and not blocking_material_or_quality
    )

    queue = _station_queue(order, tasks, pending)
    units = _packaging_units(order.id)
    unit_total = units.count()
    unit_packed = units.filter(is_packed=True).count()
    unit_shipped = units.filter(is_shipped=True).count()

    today = jdatetime.date.today()
    due_date_overdue = bool(
        order.status != 'completed'
        and order.due_date is not None
        and order.due_date < today
    )
    age_only = (
        order.status != 'completed'
        and order.due_date is None
        and age_days is not None
        and age_days > days
    )

    matched = {
        'missing_schedule': missing_schedule,
        'blocked_material': bool(blocked_material),
        'blocked_quality': bool(blocking_defects),
        'awaiting_predecessor': bool(awaiting),
        'queue_at_station': queue['shared_queue'],
        'paint_not_started': paint_not_started,
        'scheduled_overrun': bool(overdue_pending),
        'no_task_breakdown': total == 0 and order.status != 'completed',
        'packaging_pending': bool(
            unit_total and unit_packed < unit_total and done and done == total
        ),
        'shipping_pending': bool(
            unit_total and unit_shipped < unit_total and unit_packed == unit_total
        ),
        'age_only': due_date_overdue or age_only,
    }

    source_task = 'product.ProductionTask'
    cause_evidence = {
        'missing_schedule': (Evidence(
            metric='unscheduled_nonpaint_open_tasks', value=unscheduled_nonpaint_open, unit='task',
            source=f'{source_task}.scheduled_start',
            query=(
                'open non-paint tasks of this order where scheduled_start is NULL'
            ),
            derived=True,
        ),),
        'blocked_material': (Evidence(
            metric='pending_queue_rows', value=blocked_material, unit='queue row',
            source='inventory.DailyMaterialQueue',
            query=(
                "pending daily queue rows of this order where status = 'pending'"
            ),
        ),),
        'blocked_quality': (Evidence(
            metric='unresolved_defects', value=blocking_defects, unit='defect',
            source='product.ProductionDefect',
            query=(
                f'defects of this order where status in {list(OPEN_DEFECT_STATUSES)}'
            ),
        ),),
        'awaiting_predecessor': (
            Evidence(
                metric='waiting_tasks', value=waiting, unit='task',
                source=source_task,
                query="tasks of this order where status = 'waiting'",
            ),
            Evidence(
                metric='tasks_with_undone_predecessor', value=len(awaiting),
                unit='task',
                source=source_task,
                query=(
                    'waiting tasks whose (order, part, step_order - 1) predecessor is '
                    'not done (DERIVED convention, not a dependency graph)'
                ),
                derived=True,
            ),
        ),
        'queue_at_station': (
            Evidence(
                metric='pending_tasks_at_shared_station', value=queue['pending'],
                unit='task', source=source_task,
                query='pending tasks of this order at stations shared with other orders',
            ),
            Evidence(
                metric='other_orders_in_queue', value=queue['other_orders'],
                unit='order', source=source_task,
                query='distinct orders with pending tasks at the same stations',
            ),
        ),
        'paint_not_started': (Evidence(
            metric='paint_tasks_waiting',
            value=sum(1 for t in paint_rows if t['status'] == STATUS_WAITING),
            unit='task', source=source_task,
            query="paint tasks of this order where status = 'waiting'",
        ),),
        'scheduled_overrun': (Evidence(
            metric='overdue_pending_tasks', value=overdue_pending, unit='task',
            source=f'{source_task}.scheduled_end',
            query='pending tasks of this order where scheduled_end is before now',
            derived=True,
        ),),
        'no_task_breakdown': (Evidence(
            metric='tasks_total', value=total, unit='task', source=source_task,
            query='tasks of this order',
        ),),
        'packaging_pending': (
            Evidence(
                metric='packaging_units', value=unit_total, unit='unit',
                source='product.PackagingUnit',
                query='packaging units of the order items of this order',
            ),
            Evidence(
                metric='packed_units', value=unit_packed, unit='unit',
                source='product.PackagingUnit.is_packed',
                query='packaging units of this order where is_packed is true',
            ),
        ),
        'shipping_pending': (
            Evidence(
                metric='shipped_units', value=unit_shipped, unit='unit',
                source='product.PackagingUnit.is_shipped',
                query='packaging units of this order where is_shipped is true',
            ),
        ),
        'age_only': (Evidence(
            metric='order_age_days', value=age_days, unit='day',
            source='product.Order.created_at',
            query=(
                f'age of this order compared with the CraftFlow heuristic '
                f'(created_at + {days} days and not completed)'
            ),
            threshold=f'> {days} days',
        ),),
    }

    cause_summaries = {
        'missing_schedule': (
            f'{unscheduled_open} تسک باز این سفارش هیچ زمان‌بندی‌ای ندارد؛ CraftFlow '
            'زمان‌بندی را فقط برای نقاشی ثبت می‌کند.'
        ),
        'blocked_material': (
            f'{blocked_material} درخواست مواد این سفارش هنوز تحویل نشده است.'
        ),
        'blocked_quality': (
            f'{blocking_defects} خرابیِ حل‌نشده برای این سفارش ثبت شده است.'
        ),
        'awaiting_predecessor': (
            f'{len(awaiting)} تسک در انتظار مرحلهٔ قبل‌اند و پیشینِ آن‌ها done نیست.'
        ),
        'queue_at_station': (
            f'{queue["pending"]} تسک آمادهٔ انجام در صفی هستند که {queue["other_orders"]} '
            'سفارش دیگر هم در آن حضور دارند.'
        ),
        'paint_not_started': (
            f'{len(paint_rows)} تسک نقاشی وجود دارد و همهٔ آن‌ها در انتظار‌اند.'
        ),
        'scheduled_overrun': (
            f'{overdue_pending} تسک از زمان‌بندی ثبت‌شده‌شان عقب افتاده‌اند.'
        ),
        'no_task_breakdown': (
            'این سفارش هیچ تسک تولیدی ندارد، پس شکست کاری برای آن ساخته نشده است.'
        ),
        'packaging_pending': (
            f'تولید کامل است ولی {unit_total - unit_packed} واحد هنوز بسته‌بندی نشده.'
        ),
        'shipping_pending': (
            f'همهٔ واحدها بسته‌بندی شده‌اند ولی {unit_total - unit_shipped} واحد '
            'ارسال نشده است.'
        ),
        'age_only': (
            f'تاریخ تحویل سفارش گذشته است ({order.due_date.isoformat()}) و سفارش '
            'تکمیل نشده است.'
            if due_date_overdue else
            f'این سفارش {age_days} روز از تاریخ ایجاد گذشته و تکمیل نشده است؛ '
            f'تاریخ تحویل ثبت نشده و از قاعدهٔ جایگزین CraftFlow '
            f'(created_at + {days} روز) استفاده شده است.'
        ),
    }

    cause_limitations = {
        'missing_schedule': ('missing_nonpaint_schedule',),
        'blocked_material': ('missing_material_mapping',),
        'blocked_quality': ('missing_quality_events',),
        'awaiting_predecessor': ('missing_dependency_graph',),
        'queue_at_station': (),
        'paint_not_started': ('missing_dependency_graph',),
        'scheduled_overrun': ('missing_nonpaint_schedule',),
        'no_task_breakdown': (),
        'packaging_pending': (),
        'shipping_pending': (),
        'age_only': ('missing_due_date',),
    }

    limitations = ['missing_dependency_graph']
    confidence_reasons = []
    signals = [SIGNAL_REAL]

    if order.due_date is None:
        limitations.append('missing_due_date')
        signals.append(SIGNAL_MISSING)
        confidence_reasons.append(
            'Order.due_date خالی است؛ تأخیر فقط با قاعدهٔ سنی '
            f'«created_at + {days} روز» سنجیده می‌شود.'
        )
    else:
        confidence_reasons.append('Order.due_date موجود است و قابل مقایسه است.')

    if any(t['station_name'] not in STATION_CODES for t in tasks):
        limitations.append('unknown_station_codes')
        signals.append(SIGNAL_DERIVED)
        confidence_reasons.append(
            'بعضی تسک‌های این سفارش station_name نامعتبر دارند.'
        )

    if matched['blocked_material'] or matched['blocked_quality']:
        limitations.append('missing_quality_events'
                           if matched['blocked_quality'] else 'missing_material_mapping')

    evidence = [
        Evidence(
            metric='tasks_total', value=total, unit='task', source=source_task,
            query='tasks of this order grouped by status',
        ),
        Evidence(
            metric='order_age_days', value=age_days, unit='day',
            source='product.Order.created_at',
            query='age of this order in days',
            threshold=f'> {days} days (CraftFlow delayed heuristic)',
        ),
        Evidence(
            metric='due_date_available', value=order.due_date is not None,
            unit='boolean', source='product.Order.due_date',
            query='whether this order has a due_date value',
        ),
        Evidence(
            metric='due_date_overdue', value=due_date_overdue,
            unit='boolean', source='product.Order.due_date',
            query='whether today is after the order due_date',
            derived=True,
        ),
    ]

    return {
        'matched': matched,
        'age_days': age_days,
        'delay': {
            'days': days,
            'age_days': age_days,
            'rule': (
                'Order.due_date is the primary deadline when present; '
                f'otherwise CraftFlow fallback = created_at older than {days} days '
                'and status != completed'
            ),
            'rule_source': (
                'product.Order.due_date'
                if order.due_date is not None
                else 'product.views.delayed_orders'
            ),
            'due_date_used': order.due_date is not None,
            'due_date_overdue': due_date_overdue,
            'due_date_available': order.due_date is not None,
        },
        'evidence': [e.to_dict() for e in evidence],
        'cause_evidence': {k: v for k, v in cause_evidence.items() if matched[k]},
        'cause_summaries': {k: v for k, v in cause_summaries.items() if matched[k]},
        'cause_limitations': {k: v for k, v in cause_limitations.items() if matched[k]},
        'limitations': list(dict.fromkeys(limitations)),
        'confidence': confidence_from_signals(signals),
        'confidence_reasons': confidence_reasons,
    }


def _station_queue(order, tasks, pending):
    """
    آیا صف ایستگاه بین چند سفارش مشترک است؟

    یک کوئری گروه‌بندی‌شده روی کل ایستگاه‌های درگیر — نه یک کوئری به‌ازای هر
    سفارش و نه یک کوئری به‌ازای هر ایستگاه.
    """
    stations = sorted({
        t['station_name'] for t in tasks
        if t['status'] == STATUS_PENDING and t['station_name'] in STATION_CODES
    })
    if not stations:
        return {'pending': 0, 'other_orders': 0, 'shared_queue': False, 'stations': []}

    rows = (
        ProductionTask.objects
        .filter(station_name__in=stations, status=STATUS_PENDING)
        .exclude(order_id=order.id)
        .values('station_name')
        .annotate(order_count=Count('order_id', distinct=True))
    )
    other_orders = sum(row['order_count'] or 0 for row in rows)
    shared = any((row['order_count'] or 0) > 0 for row in rows)
    return {
        'pending': pending,
        'other_orders': other_orders,
        'shared_queue': shared,
        'stations': stations,
    }


__all__ = [
    'DELAYED_AFTER_DAYS',
    'DELAY_CAUSE_LABELS',
    'DELAY_CAUSE_PRECEDENCE',
    'DELAY_CAUSE_SEVERITY',
    'HEALTH_DOMAINS',
    'STALLED_AFTER_DAYS',
    'awaiting_predecessor_tasks',
    'order_age_days',
    'order_delay',
    'order_health',
    'predecessor_key',
    'successor_index',
    'task_rollup',
    'task_rows',
]