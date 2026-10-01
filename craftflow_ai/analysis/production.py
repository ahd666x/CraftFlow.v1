"""
تحلیل تولید (فاز ۲) — گلوگاه ایستگاه و سطل ایستگاه ناشناس.

این ماژول هرگز از ``craftflow_ai.tools`` import نمی‌کند؛ فقط مستقیم از ORM و
از توابع فقط‌خواندنی دامنهٔ CraftFlow (``product.models`` / ``product.utils``)
استفاده می‌کند. این همان لایهٔ پایین معماری است:

    analysis  →  services.queries / domain helpers  →  ORM

نکتهٔ صفرگردانی: ایستگاه‌هایی که ``station_name`` خارج از ``STATION_CHOICES``
دارند عمداً **حذف نمی‌شوند**؛ در سطل ``unknown_station`` گزارش می‌شوند.
"""
import jdatetime
from django.db.models import Count, F, Q
from django.utils import timezone

from craftflow_ai.analysis.evidence import (
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SIGNAL_DERIVED,
    SIGNAL_MISSING,
    SIGNAL_REAL,
    Evidence,
    Finding,
    confidence_from_signals,
)
from craftflow_ai.analysis.limits import limitation_messages

from product.models import STATION_CHOICES, Order, ProductionTask

# وضعیت‌های واقعی ProductionTask.TASK_STATUS. «در حال اجرا» و «مسدود» در مدل
# وجود ندارند و مشتق‌شده گزارش می‌شوند.
STATUS_WAITING = 'waiting'
STATUS_PENDING = 'pending'
STATUS_DONE = 'done'
OPEN_STATUSES = (STATUS_PENDING, STATUS_WAITING)

MAX_AFFECTED_ORDERS = 50

STATION_CODES = tuple(code for code, _label in STATION_CHOICES)
STATION_LABELS = {code: label for code, label in STATION_CHOICES}


def station_labels():
    return dict(STATION_LABELS)


# ----------------------------------------------------------------------
# مشتق‌های مشترک
# ----------------------------------------------------------------------

def derived_in_progress_filter(now=None):
    """pending + کارگر تخصیص‌یافته + زمان شروع گذشته."""
    now = now or timezone.now()
    return Q(status=STATUS_PENDING, assigned_worker__isnull=False,
             scheduled_start__lte=now)


def derived_blocked_filter(now=None):
    """pending + زمان پایان گذشته (عقب‌افتاده از برنامه)."""
    now = now or timezone.now()
    return Q(status=STATUS_PENDING, scheduled_end__lt=now)


# ----------------------------------------------------------------------
# سطل ایستگاه ناشناس
# ----------------------------------------------------------------------

def unknown_station_evidence() -> Evidence:
    """شواهد خامِ وجود/عدم وجود ایستگاه‌های ناشناس."""
    row = (
        ProductionTask.objects
        .exclude(station_name__in=STATION_CODES)
        .aggregate(
            total=Count('id'),
            open_tasks=Count('id', filter=Q(status__in=OPEN_STATUSES)),
        )
    )
    total = row.get('total') or 0
    open_tasks = row.get('open_tasks') or 0
    orders = (
        ProductionTask.objects
        .exclude(station_name__in=STATION_CODES)
        .exclude(status=STATUS_DONE)
        .exclude(order_id__isnull=True)
        .values('order_id')
        .distinct()
        .count()
    )
    names = sorted(
        set(
            ProductionTask.objects
            .exclude(station_name__in=STATION_CODES)
            .values_list('station_name', flat=True)
            .distinct()
        )
    )
    return Evidence(
        metric='unknown_station',
        value={
            'station_names_observed': names,
            'open_task_count': open_tasks,
            'task_count': total,
            'affected_order_count': orders,
        },
        unit='bucket',
        source='product.ProductionTask.station_name not in STATION_CHOICES',
        query='tasks grouped by station_name where station_name is outside STATION_CHOICES',
        comparison='reported separately, never merged into a known station',
        derived=True,
    )


def unknown_station_bucket(limit=20) -> dict:
    """
    سطل صریح ایستگاه‌های ناشناس.

    این سطل در هر تحلیل ایستگاه برگردانده می‌شود تا هیچ تسکی بی‌صدا حذف نشود.
    """
    ev = unknown_station_evidence()
    value = ev.value
    names = value['station_names_observed']

    rows = list(
        ProductionTask.objects
        .exclude(station_name__in=STATION_CODES)
        .values('station_name')
        .annotate(
            total=Count('id'),
            open_tasks=Count('id', filter=Q(status__in=OPEN_STATUSES)),
            done=Count('id', filter=Q(status=STATUS_DONE)),
            affected_orders=Count('order_id', distinct=True, filter=Q(status__in=OPEN_STATUSES)),
        )
        .order_by('-open_tasks', 'station_name')
    )
    for row in rows:
        row['station_label'] = None
        row['known_station'] = False

    return {
        'status': 'present' if rows else 'empty',
        'task_count': value['task_count'],
        'open_task_count': value['open_task_count'],
        'affected_order_count': value['affected_order_count'],
        'station_names_observed': names[:limit],
        'station_names_total': len(names),
        'rows': rows[:limit],
        'truncated': len(rows) > limit,
        'evidence': ev.to_dict(),
        'limitations': ['unknown_station_codes'],
        'limitations_detail': limitation_messages(['unknown_station_codes']),
        'note': (
            'مقادیر station_name خارج از STATION_CHOICES هرگز به ایستگاه معتبر نگاشت '
            'یا حذف نمی‌شوند؛ فقط به‌صورت سطل مستقل گزارش می‌شوند.'
        ),
    }


# ----------------------------------------------------------------------
# گلوگاه ایستگاه
# ----------------------------------------------------------------------

def station_bottleneck(stage: str):
    """
    تحلیل گلوگاه یک ایستگاه — همهٔ اعداد با queryهای گروه‌بندی‌شده.

    «تعداد سفارش‌های درگیر» با ``values(...).annotate(...)`` محاسبه می‌شود،
    نه با حلقهٔ ``for order in orders``؛ بنابراین الگوی N+1 تکرار نمی‌شود.
    """
    code = str(stage or '').strip()
    if code not in STATION_CODES:
        return None

    now = timezone.now()
    agg = (
        ProductionTask.objects
        .filter(station_name=code)
        .aggregate(
            pending=Count('id', filter=Q(status=STATUS_PENDING)),
            waiting=Count('id', filter=Q(status=STATUS_WAITING)),
            done=Count('id', filter=Q(status=STATUS_DONE)),
            in_progress=Count('id', filter=derived_in_progress_filter(now)),
            blocked=Count('id', filter=derived_blocked_filter(now)),
            scheduled=Count('id', filter=Q(scheduled_start__isnull=False)),
            total=Count('id'),
        )
    )
    pending = agg['pending'] or 0
    waiting = agg['waiting'] or 0
    done = agg['done'] or 0
    in_progress = agg['in_progress'] or 0
    blocked = agg['blocked'] or 0
    open_tasks = pending + waiting

    # سفارش‌های درگیر: یک کوئری گروه‌بندی‌شده، بدون N+1.
    affected_rows = list(
        ProductionTask.objects
        .filter(station_name=code, status__in=OPEN_STATUSES)
        .exclude(order_id__isnull=True)
        .values('order_id')
        .annotate(
            task_count=Count('id'),
            pending=Count('id', filter=Q(status=STATUS_PENDING)),
        )
        .order_by('-pending', '-task_count', 'order_id')
    )
    affected_order_ids = [row['order_id'] for row in affected_rows]
    affected_orders = len(affected_order_ids)

    # کارگران تخصیص‌یافته روی تسک‌های آمادهٔ انجام این ایستگاه.
    workers = list(
        ProductionTask.objects
        .filter(station_name=code, status=STATUS_PENDING,
                assigned_worker__isnull=False)
        .values('assigned_worker_id', 'assigned_worker__username',
                'assigned_worker__first_name', 'assigned_worker__last_name')
        .annotate(task_count=Count('id'))
        .order_by('-task_count', 'assigned_worker_id')
    )

    limitations = []
    signals = [SIGNAL_REAL] if (open_tasks or done) else []
    if not (open_tasks or done):
        signals = [SIGNAL_MISSING]

    if open_tasks:
        limitations.append('missing_task_timestamp')
        signals.append(SIGNAL_DERIVED)

    unknown = unknown_station_bucket()
    if unknown['open_task_count']:
        limitations.append('unknown_station_codes')
        signals.append(SIGNAL_DERIVED)

    confidence = confidence_from_signals(signals)
    confidence_reasons = _confidence_reasons(
        confidence,
        has_data=bool(open_tasks or done),
        derived_ages=bool(open_tasks),
        unknown_open=unknown['open_task_count'],
    )

    oldest_age, oldest_order = _oldest_pending_age_days(affected_order_ids)
    if oldest_age is not None and oldest_order is not None:
        age_evidence = Evidence(
            metric='oldest_pending_age_days',
            value=oldest_age,
            unit='day',
            source='product.Order.created_at (proxy for ProductionTask age)',
            query=(
                'age of the oldest Order that still has open tasks at this station'
            ),
            comparison='derived proxy: ProductionTask has no created_at field',
            threshold='>',
            derived=True,
        )
    else:
        age_evidence = None

    evidence = [
        Evidence(
            metric='open_tasks',
            value=open_tasks,
            unit='task',
            source='product.ProductionTask',
            query=f"tasks where station_name = '{code}' and status in (pending, waiting)",
        ),
        Evidence(
            metric='pending_tasks',
            value=pending,
            unit='task',
            source='product.ProductionTask',
            query=f"tasks where station_name = '{code}' and status = 'pending'",
        ),
        Evidence(
            metric='waiting_tasks',
            value=waiting,
            unit='task',
            source='product.ProductionTask',
            query=f"tasks where station_name = '{code}' and status = 'waiting'",
        ),
        Evidence(
            metric='done_tasks',
            value=done,
            unit='task',
            source='product.ProductionTask',
            query=f"tasks where station_name = '{code}' and status = 'done'",
        ),
        Evidence(
            metric='affected_orders',
            value=affected_orders,
            unit='order',
            source='product.ProductionTask',
            query=(
                f"distinct order_id of open tasks where station_name = '{code}' "
                '(grouped, not per-order queries)'
            ),
        ),
    ]
    if age_evidence is not None:
        evidence.append(age_evidence)

    findings = _station_findings(
        stage=code, open_tasks=open_tasks, blocked=blocked, waiting=waiting,
        pending=pending, affected_orders=affected_orders,
        oldest_age=oldest_age, confidence=confidence, limitations=limitations,
    )

    from craftflow_ai.analysis.limits import unavailable  # import محلی: بدون دور زدن لایه‌ها

    return {
        'stage': code,
        'stage_label': STATION_LABELS.get(code, code),
        'status': _station_status(open_tasks=open_tasks, blocked=blocked,
                                   waiting=waiting, pending=pending, done=done),
        'pending': pending,
        'waiting': waiting,
        'done': done,
        'in_progress': in_progress,
        'blocked': blocked,
        'open_tasks': open_tasks,
        'total_tasks': agg['total'] or 0,
        'scheduled_tasks': agg['scheduled'] or 0,
        'oldest_pending_age_days': oldest_age,
        'oldest_pending_order_id': oldest_order,
        'affected_orders': affected_orders,
        'affected_order_ids': affected_order_ids[:MAX_AFFECTED_ORDERS],
        'affected_orders_truncated': affected_orders > MAX_AFFECTED_ORDERS,
        'workers': [
            {
                'worker_id': w['assigned_worker_id'],
                'username': w['assigned_worker__username'],
                'display_name': (
                    (w['assigned_worker__first_name'] or '')
                    + ' ' + (w['assigned_worker__last_name'] or '')
                ).strip() or w['assigned_worker__username'],
                'assigned_task_count': w['task_count'],
            }
            for w in workers
        ],
        'workers_count': len(workers),
        'unknown_station': unknown,
        'capacity': unavailable(
            'missing_station_capacity',
            extra_blocks=('missing_nonpaint_duration',),
        ).to_dict(),
        'derived_fields': {
            'in_progress': 'pending + assigned_worker not null + scheduled_start in the past',
            'blocked': 'pending + scheduled_end in the past (behind its own schedule)',
            'open_tasks': 'pending + waiting',
            'note': (
                'در مدل ProductionTask وضعیت‌های in_progress و blocked وجود ندارند؛ '
                'هر دو مشتق‌شده‌اند.'
            ),
        },
        'confidence': confidence,
        'confidence_reasons': confidence_reasons,
        'limitations': list(dict.fromkeys(limitations + ['missing_station_capacity'])),
        'limitations_detail': limitation_messages(
            list(dict.fromkeys(limitations + ['missing_station_capacity']))
        ),
        'evidence': [e.to_dict() for e in evidence],
        'findings': [f.to_dict() for f in findings],
    }


def _station_status(*, open_tasks, blocked, waiting, pending, done):
    if open_tasks == 0 and done == 0:
        return 'not_started'
    if blocked:
        return 'blocked'
    if open_tasks == 0:
        return 'complete'
    if done and pending:
        return 'in_progress'
    if done and waiting and not pending:
        return 'blocked'
    if pending:
        return 'healthy' if done else 'warning'
    return 'blocked'


def _oldest_pending_age_days(affected_order_ids):
    """
    قدمت قدیمی‌ترین سفارشِ درگیر — DERIVED.

    ProductionTask فیلد ``created_at`` ندارد، پس قدمت تسک مستقیماً قابل
    اندازه‌گیری نیست. از قدیمی‌ترین ``Order.created_at`` به‌عنوان کران بالا
    استفاده می‌شود و این مشتق‌بودن همیشه در limitations گزارش می‌شود.
    """
    if not affected_order_ids:
        return None, None
    oldest = (
        Order.objects
        .filter(id__in=affected_order_ids)
        .order_by('created_at', 'id')
        .values('id', 'created_at')
        .first()
    )
    if not oldest or not oldest['created_at']:
        return None, None
    today = jdatetime.date.today()
    try:
        age = (today - oldest['created_at']).days
    except TypeError:
        return None, oldest['id']
    return max(int(age), 0), oldest['id']


def _confidence_reasons(confidence, *, has_data, derived_ages, unknown_open):
    if confidence == CONFIDENCE_LOW:
        return ['هیچ دادهٔ معتبری برای این ایستگاه وجود ندارد.']
    reasons = ['شمارش‌ها مستقیماً از ProductionTask آمده‌اند.']
    if derived_ages:
        reasons.append(
            'قدمت تسک مشتق‌شده است، چون ProductionTask فیلد created_at ندارد.'
        )
    if unknown_open:
        reasons.append(
            f'{unknown_open} تسک با ایستگاه ناشناس وجود دارد و روی این تحلیل اثر '
            'دامنه‌ای می‌گذارد.'
        )
    if confidence == CONFIDENCE_MEDIUM and not derived_ages and not unknown_open:
        reasons.append('بخشی از دادهٔ تصمیم مشتق‌شده است.')
    return reasons


def _station_findings(*, stage, open_tasks, blocked, waiting, pending,
                      affected_orders, oldest_age, confidence, limitations):
    findings = []
    if blocked:
        findings.append(Finding(
            finding_id=f'{stage}:scheduled_overrun',
            domain='production',
            severity='medium',
            summary=(
                f'{blocked} تسک ایستگاه {stage} از زمان‌بندی خودشان عقب افتاده‌اند.'
            ),
            evidence=(Evidence(
                metric='blocked',
                value=blocked,
                unit='task',
                source='product.ProductionTask.scheduled_end',
                query=(
                    f"pending tasks where station_name = '{stage}' and scheduled_end < now"
                ),
                derived=True,
            ),),
            confidence=confidence,
            limitations=('missing_nonpaint_schedule',),
            derived=True,
        ))
    if waiting and not pending:
        findings.append(Finding(
            finding_id=f'{stage}:awaiting_predecessor',
            domain='production',
            severity='medium',
            summary=(
                f'همهٔ {waiting} تسک باز ایستگاه {stage} در انتظار مرحلهٔ قبل‌اند.'
            ),
            evidence=(Evidence(
                metric='waiting',
                value=waiting,
                unit='task',
                source='product.ProductionTask.status',
                query=f"tasks where station_name = '{stage}' and status = 'waiting'",
            ),),
            confidence=confidence,
            limitations=('missing_dependency_graph',),
            derived=True,
        ))
    if open_tasks and affected_orders:
        findings.append(Finding(
            finding_id=f'{stage}:queue',
            domain='production',
            severity='medium' if affected_orders > 1 else 'info',
            summary=(
                f'{open_tasks} تسک باز در صف {stage} متعلق به {affected_orders} سفارش '
                'است؛ تعداد سفارش‌ها بیشتر از تعداد تسک‌ها یعنی صف چندسفارشی است.'
            ),
            evidence=(
                Evidence(
                    metric='open_tasks',
                    value=open_tasks,
                    unit='task',
                    source='product.ProductionTask',
                    query=f"open tasks where station_name = '{stage}'",
                ),
                Evidence(
                    metric='affected_orders',
                    value=affected_orders,
                    unit='order',
                    source='product.ProductionTask',
                    query=f"distinct order_id of open tasks at station '{stage}'",
                ),
            ),
            confidence=confidence,
            limitations=tuple(limitations),
            derived=bool(limitations),
        ))
    return findings


__all__ = [
    'OPEN_STATUSES',
    'STATION_CODES',
    'STATION_LABELS',
    'derived_blocked_filter',
    'derived_in_progress_filter',
    'station_bottleneck',
    'station_labels',
    'unknown_station_bucket',
]