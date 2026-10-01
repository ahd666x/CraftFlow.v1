"""
توابع فقط-خواندنی که داده‌های عملیاتی CraftFlow را برای لایهٔ AI آماده می‌کنند.

هر تابع یک ساختار ساده (dict/لیست) برمی‌گرداند؛ نه مدل، نه Response.
هیچ‌کدام از این توابع چیزی در دیتابیس نمی‌نویسد.
"""
from datetime import timedelta
from decimal import Decimal

import jdatetime
from django.db.models import Case, Count, DecimalField, F, Q, Sum, Value, When
from django.utils import timezone

from inventory.models import (
    MaterialCustody,
    MaterialIssue,
    MaterialLeftover,
    RawMaterial,
)
from product.models import (
    STATION_CHOICES,
    Order,
    PackagingUnit,
    ProductionDefect,
    ProductionTask,
)

# همان قاعدهٔ ``product.views.delayed_orders``: «عقب‌افتاده» یعنی قدیمی‌تر از
# N روز و ناتمام. عمداً همان تعریف پروژه استفاده می‌شود، نه due_date.
DELAYED_AFTER_DAYS = 3

# وضعیت‌های واقعی تسک در این پروژه (product.models.ProductionTask.TASK_STATUS).
# «در حال اجرا» و «مسدود» در مدل وجود ندارند؛ بنابراین به‌صورت مشتق‌شده و
# با برچسب derived گزارش می‌شوند تا با دیتابیس اشتباه نشوند.
STATUS_WAITING = 'waiting'
STATUS_PENDING = 'pending'
STATUS_DONE = 'done'

# فیلد پولی/مقداری با دقت مطابق RawMaterial و StockMovement.
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


# ----------------------------------------------------------------------
# کمکی‌های عمومی
# ----------------------------------------------------------------------

def station_map():
    """نگاشت کد ایستگاه → برچسب فارسی، مستقیماً از مدل."""
    return {code: label for code, label in STATION_CHOICES}


def jalali_today():
    return jdatetime.date.today()


def fmt_jalali(value):
    if value is None:
        return None
    try:
        return value.strftime('%Y/%m/%d')
    except (AttributeError, ValueError):
        return str(value)


def dec(value):
    """تبدیل امن Decimal به رشته برای خروجی JSON."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def money(value):
    """
    قالب‌بندی مقدار با دو رقم اعشار.

    SQLite مقیاس اعشار را در annotate حفظ نمی‌کند (‎7 به‌جای 7.00)، پس نمایش
    یکدست اینجا و نه در لایهٔ دیتابیس انجام می‌شود.
    """
    if value is None:
        return None
    try:
        return str(Decimal(value).quantize(Decimal('0.01')))
    except (TypeError, ValueError, ArithmeticError):
        return str(value)


def active_statuses():
    return [code for code, _ in Order.ORDER_STATUS if code != 'completed']


# ----------------------------------------------------------------------
# سفارش‌ها
# ----------------------------------------------------------------------

def open_orders(limit=25):
    """
    سفارش‌های باز — همان تعریفی که ``product.views.dashboard`` با
    ``active_orders`` استفاده می‌کند (draft / planned / producing).
    """
    limit = max(1, min(int(limit or 25), 200))
    qs = (
        Order.objects
        .filter(status__in=active_statuses())
        .select_related('customer', 'user')
        .order_by('priority', 'id')
    )
    total = qs.count()
    rows = []
    for order in qs[:limit]:
        rows.append(_order_brief(order))
    return {
        'orders': rows,
        'total_open_orders': total,
        'returned': len(rows),
        'truncated': total > len(rows),
        'statuses': list(active_statuses()),
    }


def _order_brief(order):
    tasks = order.tasks.all()
    total = tasks.count()
    done = tasks.filter(status=STATUS_DONE).count()
    return {
        'order_id': order.id,
        'order_number': order.number or None,
        'customer': order.customer.name,
        'status': order.status,
        'status_label': order.get_status_display(),
        'priority': order.priority,
        'created_at': fmt_jalali(order.created_at),
        'due_date': fmt_jalali(order.due_date),
        'tasks_total': total,
        'tasks_done': done,
        'tasks_remaining': max(total - done, 0),
        'progress_percent': int(round(done * 100 / total)) if total else 0,
    }


def order_details(order_id):
    """اطلاعات کامل عملیاتی یک سفارش (فقط خواندنی)."""
    try:
        order = (
            Order.objects
            .select_related('customer', 'user')
            .prefetch_related(
                'items__product__category',
                'items__ordercolor',
                'items__packaging_units',
            )
            .get(pk=order_id)
        )
    except Order.DoesNotExist:
        return None
    except (ValueError, TypeError):
        return None

    labels = station_map()
    tasks = list(
        order.tasks
        .select_related('part', 'assigned_worker', 'painting_stage__process')
        .order_by('step_order')
    )

    per_station = []
    for code, station_label in STATION_CHOICES:
        station_tasks = [t for t in tasks if t.station_name == code]
        if not station_tasks:
            continue
        done = sum(1 for t in station_tasks if t.status == STATUS_DONE)
        per_station.append({
            'stage': code,
            'stage_label': labels.get(code, code),
            'total': len(station_tasks),
            'done': done,
            'pending': sum(1 for t in station_tasks if t.status == STATUS_PENDING),
            'waiting': sum(1 for t in station_tasks if t.status == STATUS_WAITING),
        })

    total = len(tasks)
    done = sum(1 for t in tasks if t.status == STATUS_DONE)

    current_task = next((t for t in tasks if t.status == STATUS_PENDING), None)
    waiting_count = sum(1 for t in tasks if t.status == STATUS_WAITING)

    items = []
    for item in order.items.all():
        items.append({
            'order_item_id': item.id,
            'product': item.product.name,
            'category': item.product.category.name if item.product.category_id else None,
            'quantity': item.quantity,
            'size': item.size or None,
            'color_summary': item.color_summary,
            'packaging': _packaging_for_item(item),
        })

    defects = (
        ProductionDefect.objects
        .filter(order_id=order.id)
        .values('status')
        .annotate(count=Count('id'))
    )
    open_issues = list(
        MaterialIssue.objects
        .filter(
            Q(task__order_id=order.id) | Q(order_item__order_id=order.id)
        )
        .exclude(status='cancelled')
        .values('id', 'status', 'purpose', 'raw_material_id')
    )

    return {
        'order_id': order.id,
        'order_number': order.number or None,
        'customer': order.customer.name,
        'status': order.status,
        'status_label': order.get_status_display(),
        'priority': order.priority,
        'priority_label': dict(Order._meta.get_field('priority').choices).get(order.priority),
        'created_at': fmt_jalali(order.created_at),
        'due_date': fmt_jalali(order.due_date),
        'items': items,
        'items_count': len(items),
        'tasks_total': total,
        'tasks_done': done,
        'tasks_remaining': max(total - done, 0),
        'progress_percent': int(round(done * 100 / total)) if total else 0,
        'stages': per_station,
        'current_stage': (
            labels.get(current_task.station_name, current_task.station_name)
            if current_task else None
        ),
        'current_task_id': current_task.id if current_task else None,
        'waiting_task_count': waiting_count,
        'packaging': order.packaging_summary,
        'defects': {row['status']: row['count'] for row in defects},
        'open_material_issues': len(open_issues),
        'has_tasks': total > 0,
    }


def _packaging_for_item(item):
    packed, total = item.packaging_progress
    shipped, _ = item.shipping_progress
    return {
        'units': total,
        'packed': packed,
        'shipped': shipped,
    }


def delayed_orders(limit=25, days=None):
    """همان منطق ``product.views.delayed_orders`` با امکان تعیین سقف."""
    days = DELAYED_AFTER_DAYS if days is None else max(0, int(days))
    limit = max(1, min(int(limit or 25), 200))
    limit_date = timezone.now() - timedelta(days=days)

    qs = (
        Order.objects
        .filter(created_at__lt=limit_date)
        .exclude(status='completed')
        .select_related('customer', 'user')
        .order_by('priority', 'created_at')
    )
    total = qs.count()
    rows = []
    for order in qs[:limit]:
        brief = _order_brief(order)
        brief['days_open'] = max(
            (jdatetime.date.today() - order.created_at).days if order.created_at else 0,
            0,
        )
        brief['overdue_vs_due_date'] = bool(
            order.due_date and order.due_date < jdatetime.date.today()
        )
        brief['delayed_by_definition'] = f'older_than_{days}_days_and_not_completed'
        rows.append(brief)

    return {
        'delayed_orders': rows,
        'total_delayed': total,
        'returned': len(rows),
        'truncated': total > len(rows),
        'rule': {
            'source': 'product.views.delayed_orders',
            'created_older_than_days': days,
            'excludes_status': 'completed',
            'note': 'due_date در این گزارش ملاک تأخیر نیست.',
        },
    }


# ----------------------------------------------------------------------
# تولید
# ----------------------------------------------------------------------

def station_load():
    """
    بار کاری هر ایستگاه — همان شکل ``dashboard.station_load`` ولی کامل‌تر.

    «در حال اجرا» و «مسدود» در مدل وجود ندارند و مشتق می‌شوند:
      * in_progress: pending و کارگر تخصیص‌یافته و زمان شروع گذشته
      * blocked:     pending با زمان پایان گذشته (یعنی از برنامه عقب افتاده)
    """
    now = timezone.now()
    codes = [code for code, _ in STATION_CHOICES]
    labels = station_map()

    rows = (
        ProductionTask.objects
        .filter(station_name__in=codes)
        .values('station_name')
        .annotate(
            total=Count('id'),
            pending=Count('id', filter=Q(status=STATUS_PENDING)),
            waiting=Count('id', filter=Q(status=STATUS_WAITING)),
            done=Count('id', filter=Q(status=STATUS_DONE)),
            in_progress=Count(
                'id',
                filter=Q(status=STATUS_PENDING, assigned_worker__isnull=False,
                         scheduled_start__lte=now),
            ),
            blocked=Count(
                'id',
                filter=Q(status=STATUS_PENDING, scheduled_end__lt=now),
            ),
            scheduled=Count('id', filter=Q(scheduled_start__isnull=False)),
        )
    )
    by_code = {row['station_name']: row for row in rows}

    stations = []
    for code, label in STATION_CHOICES:
        data = by_code.get(code, {})
        stations.append({
            'stage': code,
            'stage_label': label,
            'pending': data.get('pending', 0),
            'waiting': data.get('waiting', 0),
            'in_progress': data.get('in_progress', 0),
            'blocked': data.get('blocked', 0),
            'done': data.get('done', 0),
            'total': data.get('total', 0),
            'open_tasks': data.get('pending', 0) + data.get('waiting', 0),
            'has_data': code in by_code,
        })

    return {
        'stations': stations,
        'open_tasks': sum(s['open_tasks'] for s in stations),
        'pending_tasks': sum(s['pending'] for s in stations),
        'waiting_tasks': sum(s['waiting'] for s in stations),
        'in_progress_tasks': sum(s['in_progress'] for s in stations),
        'blocked_tasks': sum(s['blocked'] for s in stations),
        'completed_tasks': sum(s['done'] for s in stations),
        'derived_fields': {
            'in_progress': 'pending + assigned_worker + scheduled_start گذشته',
            'blocked': 'pending + scheduled_end گذشته (عقب‌افتاده از برنامه)',
            'note': 'در مدل ProductionTask وضعیت in_progress/blocked وجود ندارد.',
        },
        'busiest_stage': _busiest(stations),
    }


def _busiest(stations):
    candidates = [s for s in stations if s['open_tasks'] > 0]
    if not candidates:
        return None
    top = max(candidates, key=lambda s: s['open_tasks'])
    return {
        'stage': top['stage'],
        'stage_label': top['stage_label'],
        'open_tasks': top['open_tasks'],
    }


def production_status():
    """وضعیت کلی تولید — داشبورد واقعی، بدون ساختن داده."""
    stations = station_load()

    orders_total = Order.objects.count()
    by_status = list(
        Order.objects
        .values('status')
        .annotate(count=Count('id'))
        .order_by('status')
    )
    status_map = {code: 0 for code, _ in Order.ORDER_STATUS}
    for row in by_status:
        status_map[row['status']] = row['count']

    today_gregorian = jdatetime.date.today().togregorian()
    shipped_today = PackagingUnit.objects.filter(
        is_shipped=True, shipped_at__date=today_gregorian
    ).count()
    packed_today = PackagingUnit.objects.filter(
        is_packed=True, packed_at__date=today_gregorian
    ).count()

    open_defects = ProductionDefect.objects.exclude(status='closed').count()

    return {
        'today': fmt_jalali(jdatetime.date.today()),
        'orders': {
            'total': orders_total,
            'draft': status_map.get('draft', 0),
            'planned': status_map.get('planned', 0),
            'producing': status_map.get('producing', 0),
            'completed': status_map.get('completed', 0),
            'active': sum(status_map.get(c, 0) for c in active_statuses()),
        },
        'tasks': {
            'pending': stations['pending_tasks'],
            'waiting': stations['waiting_tasks'],
            'in_progress': stations['in_progress_tasks'],
            'blocked': stations['blocked_tasks'],
            'completed': stations['completed_tasks'],
            'open_total': stations['open_tasks'],
        },
        'stations': stations['stations'],
        'busiest_stage': stations['busiest_stage'],
        'packaging': {
            'packed_today': packed_today,
            'shipped_today': shipped_today,
        },
        'quality': {'open_defects': open_defects},
        'derived_fields': stations['derived_fields'],
    }


def pending_tasks(stage=None, order_id=None, limit=25):
    """تسک‌های آمادهٔ انجام (``status='pending'``) با فیلتر اختیاری."""
    limit = max(1, min(int(limit or 25), 200))
    qs = ProductionTask.objects.filter(status=STATUS_PENDING)

    labels = station_map()
    if stage:
        valid = [code for code, _ in STATION_CHOICES]
        if stage not in valid:
            return {
                'tasks': [], 'total': 0, 'error': True,
                'available_stages': [
                    {'stage': c, 'stage_label': labels.get(c, c)} for c in valid
                ],
            }
        qs = qs.filter(station_name=stage)
    if order_id:
        qs = qs.filter(order_id=order_id)

    qs = qs.select_related(
        'order', 'part', 'order_item__product', 'assigned_worker', 'painting_stage__process'
    ).order_by('order_id', 'step_order')

    total = qs.count()
    rows = []
    for task in qs[:limit]:
        rows.append({
            'task_id': task.id,
            'order_id': task.order_id,
            'stage': task.station_name,
            'stage_label': task.get_station_name_display(),
            'step_order': task.step_order,
            'quantity': task.quantity,
            'completed_quantity': task.completed_quantity,
            'part': (task.part.name if task.part_id else None),
            'product': (
                task.order_item.product.name
                if task.order_item_id and task.order_item.product_id else None
            ),
            'assigned_worker': (
                task.assigned_worker.get_full_name() or task.assigned_worker.username
                if task.assigned_worker_id else None
            ),
            'painting_stage': (
                task.painting_stage.name if task.painting_stage_id else None
            ),
            'scheduled_start': task.scheduled_start.isoformat() if task.scheduled_start else None,
            'scheduled_end': task.scheduled_end.isoformat() if task.scheduled_end else None,
        })

    return {
        'tasks': rows,
        'total': total,
        'returned': len(rows),
        'truncated': total > len(rows),
        'filter_stage': stage,
        'filter_order_id': order_id,
    }


def bottlenecks(limit=5):
    """
    گلوگاه‌ها بر پایهٔ دادهٔ واقعی: برای هر ایستگاه، تعداد pending/waiting/
    در‌حال‌اجرا/مسدود و قدیمی‌ترین تسک آمادهٔ انجام.

    تفسیر و اولویت‌بندی نهایی را AI انجام می‌دهد؛ اینجا فقط داده است.
    """
    now = timezone.now()
    codes = [code for code, _ in STATION_CHOICES]
    labels = station_map()

    agg = (
        ProductionTask.objects
        .filter(station_name__in=codes)
        .values('station_name')
        .annotate(
            pending=Count('id', filter=Q(status=STATUS_PENDING)),
            waiting=Count('id', filter=Q(status=STATUS_WAITING)),
            done=Count('id', filter=Q(status=STATUS_DONE)),
            in_progress=Count(
                'id',
                filter=Q(status=STATUS_PENDING, assigned_worker__isnull=False,
                         scheduled_start__lte=now),
            ),
            blocked=Count(
                'id', filter=Q(status=STATUS_PENDING, scheduled_end__lt=now)
            ),
        )
    )
    by_code = {row['station_name']: row for row in agg}

    # قدیمی‌ترین تسک آمادهٔ انجام برای هر ایستگاه (یک کوئری، نه N+1)
    pending_qs = (
        ProductionTask.objects
        .filter(station_name__in=codes, status=STATUS_PENDING)
        .order_by('station_name', 'order_id', 'step_order')
        .values('station_name', 'id', 'order_id', 'part__name', 'quantity', 'scheduled_start')
    )
    oldest = {}
    for row in pending_qs:
        oldest.setdefault(row['station_name'], row)

    result = []
    for code, label in STATION_CHOICES:
        data = by_code.get(code)
        if not data:
            continue
        open_tasks = data['pending'] + data['waiting']
        if open_tasks == 0:
            continue

        old = oldest.get(code)
        reasons = []
        if data['blocked']:
            reasons.append(f"{data['blocked']} تسک از زمان‌بندی گذشته")
        if data['waiting']:
            reasons.append(f"{data['waiting']} تسک در انتظار مرحلهٔ قبل")
        if data['pending']:
            reasons.append(f"{data['pending']} تسک آمادهٔ انجام")
        if not reasons:
            reasons.append('بدون صف باز')

        result.append({
            'stage': code,
            'stage_label': label,
            'pending_tasks': data['pending'],
            'waiting_tasks': data['waiting'],
            'in_progress_tasks': data['in_progress'],
            'blocked_tasks': data['blocked'],
            'completed_tasks': data['done'],
            'open_tasks': open_tasks,
            'oldest_pending': (
                {
                    'task_id': old['id'],
                    'order_id': old['order_id'],
                    'part': old['part__name'],
                    'quantity': old['quantity'],
                    'scheduled_start': (
                        old['scheduled_start'].isoformat() if old['scheduled_start'] else None
                    ),
                }
                if old else None
            ),
            'reason': '؛ '.join(reasons),
            'load_ratio': round(open_tasks / max(done_total(by_code, code), 1), 2),
        })

    result.sort(key=lambda r: (r['open_tasks'], r['blocked_tasks']), reverse=True)
    limit = max(1, min(int(limit or 5), 20))
    top = result[:limit]

    return {
        'bottlenecks': top,
        'total_stages_with_queue': len(result),
        'returned': len(top),
        'derived_fields': {
            'blocked': 'pending + scheduled_end گذشته؛ در مدل وضعیت مسدود وجود ندارد.',
        },
        'threshold_note': 'همهٔ ایستگاه‌های دارای صف باز فهرست شده‌اند؛ مرتب‌سازی بر اساس حجم صف.',
    }


def done_total(by_code, code):
    return (by_code.get(code) or {}).get('done', 0)


# ----------------------------------------------------------------------
# انبار
# ----------------------------------------------------------------------

def _stock_expression():
    """
    همان CASE واقعی موجودی: فقط ``consumption`` کم می‌کند.

    چون اینجا روی خود ``RawMaterial``annotate می‌شود (نه روی ``movements``)،
    باید پیشوند رابطه یعنی ``movements__`` در نام فیلدها بیاید — دقیقاً همان
    چیزی که ``inventory/views.py`` در annotate خودش نوشته است.
    """
    return Sum(
        Case(
            When(movements__movement_type='consumption', then=-F('movements__quantity')),
            default=F('movements__quantity'),
            # decimal_places همان چیزی است که خود StockMovement.quantity دارد
            # (۱۲ رقم با ۲ رقم اعشار) تا خروجی عددی یکدست بماند.
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
    )


def inventory_status(limit=200):
    """
    وضعیت موجودی — از همان منطق ``inventory.views.low_stock_report``:
    موجودی = مجموع گردش‌ها، و «کم» یعنی ``<= min_stock_alert``.
    """
    from django.db.models.functions import Coalesce

    rows = (
        RawMaterial.objects
        .filter(is_active=True)
        .select_related('category')
        .annotate(stock=Coalesce(
                _stock_expression(), Value(Decimal('0'), output_field=MONEY_FIELD)
            ))
        .order_by('category__name', 'name')
    )
    total_materials = rows.count()

    # شمارش کم‌موجود/ناموجود باید روی *کل* انبار باشد، نه فقط ردیف‌هایی که در
    # ``limit`` جا شده‌اند؛ وگرنه با limit کوچک، شمارنده‌ها گمراه‌کننده می‌شوند
    # (همان قاعدهٔ ``low_stock_report`` که روی کل queryset فیلتر می‌کند).
    stock_of_rows = rows
    counts = stock_of_rows.aggregate(
        out_of_stock=Count('id', filter=Q(stock__lte=0)),
        low=Count('id', filter=Q(stock__gt=0, stock__lte=F('min_stock_alert'))),
    )
    total_out_of_stock = counts.get('out_of_stock') or 0
    total_low = counts.get('low') or 0

    items = []
    low, out = [], []

    for raw in rows[:max(1, min(int(limit or 200), 1000))]:
        stock = raw.stock or Decimal('0')
        entry = {
            'raw_material_id': raw.id,
            'name': raw.name,
            'category': raw.category.name,
            'unit': raw.get_unit_display(),
            'stock': money(stock),
            'min_stock_alert': money(raw.min_stock_alert),
            'pack_size': money(raw.pack_size),
            'status': 'out_of_stock' if stock <= 0 else (
                'low' if stock <= raw.min_stock_alert else 'ok'
            ),
        }
        items.append(entry)
        if entry['status'] == 'out_of_stock':
            out.append(entry)
        elif entry['status'] == 'low':
            low.append(entry)

    open_issues = list(
        MaterialIssue.objects
        .filter(status__in=['requested', 'partial'])
        .values('status')
        .annotate(count=Count('id'))
    )
    issue_map = {row['status']: row['count'] for row in open_issues}

    custody_total = (
        MaterialCustody.objects
        .filter(quantity__gt=0)
        .aggregate(total=Sum('quantity'))['total'] or Decimal('0')
    )
    floor_leftover = (
        MaterialLeftover.objects
        .aggregate(total=Sum('quantity'))['total'] or Decimal('0')
    )

    return {
        'total_materials': total_materials,
        'returned': len(items),
        'truncated': total_materials > len(items),
        'low_stock_count': total_low,
        'out_of_stock_count': total_out_of_stock,
        'low_stock_materials': low[:50],
        'out_of_stock_materials': out[:50],
        'counts_note': (
            'شمارنده‌های کم‌موجود/ناموجود روی کل انبار محاسبه می‌شوند، نه فقط '
            'ردیف‌های بازگشتی؛ پس با limit کوچک هم درست‌اند.'
        ),
        'materials': items,
        'open_material_issues': {
            'requested': issue_map.get('requested', 0),
            'partial': issue_map.get('partial', 0),
            'total': issue_map.get('requested', 0) + issue_map.get('partial', 0),
        },
        'floor_leftovers': {
            'custody_total': money(custody_total),
            'production_floor_total': money(floor_leftover),
        },
        'stock_rule': 'stock = مجموع گردش‌ها؛ فقط movement_type=consumption کم می‌کند.',
    }


def order_material_requirements(order_id):
    """
    مواد مورد نیاز یک سفارش، از دو منبع واقعی:

    1. ``MaterialIssue`` — درخواست‌های واقعی ثبت‌شده در انبار (دقیق‌ترین منبع)
    2. ``_task_material_requirements`` — نیاز محاسبه‌شده از BOM هر تسک باز
       (همان تابعی که صف تحویل انبار از آن استفاده می‌کند)
    """
    from inventory.views import _task_material_requirements

    try:
        order = Order.objects.prefetch_related(
            'items__product', 'tasks__part__material__raw_material',
            'tasks__order_item__product', 'tasks__painting_stage__process',
        ).get(pk=order_id)
    except Order.DoesNotExist:
        return None
    except (ValueError, TypeError):
        return None

    issues = list(
        MaterialIssue.objects
        .filter(Q(task__order_id=order.id) | Q(order_item__order_id=order.id))
        .select_related('raw_material', 'raw_material__category', 'task', 'painting_process')
        .order_by('status', '-created_at')
    )

    issue_rows = [{
        'issue_id': i.id,
        'raw_material_id': i.raw_material_id,
        'raw_material': i.raw_material.name,
        'category': (
            i.raw_material.category.name if i.raw_material.category_id else None
        ),
        'requested_quantity': money(i.requested_quantity),
        'issued_quantity': money(i.issued_quantity),
        'remaining_quantity': money(
            (i.requested_quantity or Decimal('0')) - (i.issued_quantity or Decimal('0'))
        ),
        'status': i.status,
        'status_label': i.get_status_display(),
        'purpose': i.purpose,
        'color_part': i.color_part or None,
        'task_id': i.task_id,
        'order_item_id': i.order_item_id,
    } for i in issues]

    # نیاز محاسبه‌شده از BOM برای تسک‌های هنوز انجام‌نشده
    computed = {}
    tasks = (
        order.tasks
        .filter(status__in=[STATUS_PENDING, STATUS_WAITING])
        .select_related('part__material__raw_material', 'painting_stage__process')
    )
    for task in tasks:
        try:
            rows = _task_material_requirements(task)
        except Exception:
            continue
        for raw, qty in rows:
            key = raw.id
            bucket = computed.setdefault(key, {
                'raw_material_id': raw.id,
                'raw_material': raw.name,
                'unit': raw.get_unit_display(),
                'required_quantity': Decimal('0'),
                'task_count': 0,
            })
            bucket['required_quantity'] += Decimal(qty or 0)
            bucket['task_count'] += 1

    computed_rows = [
        {**v, 'required_quantity': money(v['required_quantity'])}
        for v in sorted(
            computed.values(), key=lambda x: x['required_quantity'], reverse=True
        )
    ]

    open_issues = [r for r in issue_rows if r['status'] in ('requested', 'partial')]

    return {
        'order_id': order.id,
        'order_number': order.number or None,
        'order_status': order.status,
        'customer': order.customer.name if order.customer_id else None,
        'material_issues': issue_rows,
        'open_issue_count': len(open_issues),
        'requested_from_bom': computed_rows,
        'has_data': bool(issue_rows or computed_rows),
        'source_note': (
            'material_issues از درخواست‌های واقعی انبار؛ '
            'requested_from_bom از فرمول ساخت (BOM) تسک‌های انجام‌نشده محاسبه شده است.'
        ),
    }


def material_shortages(limit=50):
    """
    کمبود مواد — با همان ریاضیات واقعی ``inventory.services.build_plans``
    (باقی‌ماندهٔ سالن + گِرد کردن به بسته)، نه یک محاسبهٔ ساده و ناقص.
    """
    from inventory.services import build_plans

    issues = list(
        MaterialIssue.objects
        .filter(status__in=['requested', 'partial'])
        .select_related('raw_material')
        .order_by('raw_material_id', 'id')
    )
    if not issues:
        return {
            'shortages': [],
            'checked_issues': 0,
            'shortage_count': 0,
            'plan_logic': 'inventory.services.build_plans',
        }

    issues_qty = []
    for issue in issues:
        remaining = (issue.requested_quantity or Decimal('0')) - (
            issue.issued_quantity or Decimal('0')
        )
        if remaining > 0:
            issues_qty.append((issue, remaining))

    if not issues_qty:
        return {
            'shortages': [], 'checked_issues': len(issues),
            'shortage_count': 0, 'plan_logic': 'inventory.services.build_plans',
        }

    raw_ids = {issue.raw_material_id for issue, _ in issues_qty}
    leftovers = {
        row.raw_material_id: row.quantity
        for row in MaterialLeftover.objects.filter(raw_material_id__in=raw_ids)
    }

    plans = build_plans(issues_qty, leftovers)

    short = [p for p in plans if not p['enough']]
    short.sort(key=lambda p: (p['stock'] - p['physical']))
    limit = max(1, min(int(limit or 50), 200))

    rows = [{
        'raw_material_id': p['raw_material_id'],
        'raw_material': p['raw'].name,
        'unit': p['raw'].get_unit_display(),
        'needed': money(p['need']),
        'from_leftover': money(p['from_leftover']),
        'from_stock': money(p['from_stock']),
        'physical_required': money(p['physical']),
        'packs': p['packs'],
        'pack_size': money(p['pack_size']),
        'current_stock': money(p['stock']),
        'shortage_amount': money(p['physical'] - p['stock']),
        'issue_ids': p['issue_ids'],
        'reason': (
            f"نیاز فیزیکی {p['physical']} {p['raw'].get_unit_display()} "
            f"و موجودی انبار {p['stock']}"
        ),
    } for p in short[:limit]]

    return {
        'shortages': rows,
        'checked_issues': len(issues_qty),
        'shortage_count': len(short),
        'returned': len(rows),
        'truncated': len(short) > len(rows),
        'plan_logic': 'inventory.services.build_plans (باقی‌ماندهٔ سالن + گِرد کردن به بسته)',
        'note': 'موجودی کسرشده از باقی‌ماندهٔ سالن تولید است، نه امانت کارگران.',
    }


# ----------------------------------------------------------------------
# کیفیت
# ----------------------------------------------------------------------

def quality_status(limit=50):
    """خلاصهٔ خرابی‌های ثبت‌شده در تولید."""
    by_status = list(
        ProductionDefect.objects
        .values('status')
        .annotate(count=Count('id'))
    )
    status_map = {row['status']: row['count'] for row in by_status}

    rows = list(
        ProductionDefect.objects
        .select_related('order', 'order_item__product')
        .order_by('-created_at')[:max(1, min(int(limit or 50), 200))]
    )

    return {
        'total_open': sum(
            c for s, c in status_map.items() if s != 'closed'
        ),
        'total_all': sum(status_map.values()),
        'by_status': status_map,
        'recent': [{
            'defect_id': d.id,
            'order_id': d.order_id,
            'quantity': d.quantity,
            'status': d.status,
            'status_label': d.get_status_display(),
            'color_part': d.color_part or None,
            'process': d.process_name,
            'created_at': d.created_at.isoformat() if d.created_at else None,
        } for d in rows],
    }


# ----------------------------------------------------------------------
# گزارش ترکیبی
# ----------------------------------------------------------------------

def production_report():
    """گزارش یکپارچهٔ تولید — فقط از همین توابع فقط-خواندنی ساخته می‌شود."""
    status = production_status()
    necks = bottlenecks(limit=5)
    delayed = delayed_orders(limit=10)
    shortages = material_shortages(limit=10)
    open_orders_data = open_orders(limit=10)

    return {
        'generated_at': timezone.now().isoformat(),
        'today': status['today'],
        'production': status,
        'bottlenecks': necks['bottlenecks'],
        'delayed_orders': delayed['delayed_orders'],
        'delayed_total': delayed['total_delayed'],
        'material_shortages': shortages['shortages'],
        'shortage_total': shortages['shortage_count'],
        'open_orders': open_orders_data['orders'],
        'open_orders_total': open_orders_data['total_open_orders'],
        'quality': status['quality'],
        'sources': [
            'production.production_status',
            'production.bottlenecks',
            'orders.delayed_orders',
            'inventory.material_shortages',
            'orders.open_orders',
        ],
    }


__all__ = [
    'DELAYED_AFTER_DAYS',
    'bottlenecks',
    'delayed_orders',
    'inventory_status',
    'jalali_today',
    'material_shortages',
    'open_orders',
    'order_details',
    'order_material_requirements',
    'pending_tasks',
    'production_report',
    'production_status',
    'quality_status',
    'station_load',
]