import json
import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Q, Sum, Count, F, Case, When, Value, CharField, ProtectedError, DecimalField
from django.db.models.functions import Coalesce
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.utils import timezone
from django.shortcuts import get_object_or_404, render, redirect
from django.template.loader import render_to_string
from django.db.transaction import atomic
from django.views.decorators.http import require_http_methods, require_POST

from product.decorators import admin_or_manager_required, warehouse_or_manager_required
from .models import (
    Supplier, RawMaterialCategory, RawMaterial,
    StockMovement, PurchaseOrder, PurchaseOrderItem,
    MaterialCustody, MaterialIssue, DailyMaterialQueue,
    DailyMaterialClosing,
)
from .forms import (
    SupplierForm, RawMaterialCategoryForm, RawMaterialForm,
    StockMovementForm, PurchaseOrderForm, PurchaseOrderItemForm
)

from . import services
from .services import HandoverError


logger = logging.getLogger(__name__)


# ============================================================
# Helper
# ============================================================

def _inventory_context(active_tab='dashboard'):
    return {
        'active_tab': active_tab,
    }


def _no_store(response):
    """
    این صفحات دادهٔ لحظه‌ای صف/امانت را نشان می‌دهند؛ هیچ‌کدام نباید
    توسط مرورگر یا پراکسی کش شوند وگرنه کاربر نسخهٔ قدیمیِ JS را می‌بیند
    و تغییرات «اعمال نمی‌شوند».
    """
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


def _page_version():
    """شناسهٔ نسخهٔ کد تا کاربر بتواند ببیند آخرین تغییرات را می‌بیند یا نه."""
    import hashlib
    import os

    marks = []
    for rel in ('views.py', 'services.py', 'models.py'):
        path = os.path.join(os.path.dirname(__file__), rel)
        try:
            with open(path, 'rb') as fh:
                marks.append(hashlib.sha1(fh.read()).hexdigest()[:8])
        except OSError:
            marks.append('00000000')
    return '-'.join(marks)


def _paint_workers():
    """کاربران فعالِ مرحلهٔ نقاشی؛ اگر پروفایل کارگری وجود نداشت، همهٔ کاربران فعال."""
    workers = User.objects.filter(
        is_active=True, workerprofile__stage='paint'
    ).order_by('first_name', 'last_name', 'username')
    workers = list(workers)
    if workers:
        return workers
    return list(User.objects.filter(is_active=True).order_by('first_name', 'last_name', 'username'))


def _open_custody_users():
    """کاربرانی که هم‌اکنون بستهٔ بازِ امانت‌شده دستشان است."""
    ids = (
        MaterialCustody.objects.filter(quantity__gt=0)
        .values_list('held_by_id', flat=True)
        .distinct()
    )
    return list(
        User.objects.filter(pk__in=list(ids)).order_by('first_name', 'last_name', 'username')
    )


# ============================================================
# Dashboard
# ============================================================

@login_required
@warehouse_or_manager_required
def inventory_dashboard(request):
    total_materials = RawMaterial.objects.filter(is_active=True).count()
    total_suppliers = Supplier.objects.filter(is_active=True).count()
    low_stock = RawMaterial.objects.filter(is_active=True).annotate(
        stock=Coalesce(
            Sum(
                Case(
                    When(movements__movement_type='consumption', then=-F('movements__quantity')),
                    default=F('movements__quantity'),
                    output_field=DecimalField()
                )
            ),
            Value(0, output_field=DecimalField())
        )
    ).filter(stock__lte=F('min_stock_alert')).count()

    recent_movements = StockMovement.objects.select_related(
        'raw_material', 'supplier', 'created_by'
    ).order_by('-created_at')[:10]

    pending_orders = PurchaseOrder.objects.filter(status__in=['draft', 'ordered']).count()
    pending_issues = MaterialIssue.objects.filter(
        status='requested',
    ).filter(
        Q(task__isnull=False) | Q(defect__isnull=False)
    ).count()

    context = {
        **_inventory_context('dashboard'),
        'total_materials': total_materials,
        'total_suppliers': total_suppliers,
        'low_stock': low_stock,
        'recent_movements': recent_movements,
        'pending_orders': pending_orders,
        'pending_issues': pending_issues,
    }
    return render(request, 'inventory/dashboard.html', context)


# ============================================================
# Production material hand-over
# ============================================================

def _task_material_requirements(task):
    """Return material consumption rows for one production task.

    For paint tasks the requirements come from PaintingMaterialRequirement
    (keyed on PaintingStage, NOT on Part/BOM). For all other stations the
    requirement is derived from the Part's Material/RawMaterial.
    """
    rows = []

    if task.station_name == 'paint':
        if not task.painting_stage_id:
            return rows
        from product.utils import get_painting_material_requirements_for_task
        requirements = get_painting_material_requirements_for_task(task)
        for req in requirements:
            rows.append((req.raw_material, Decimal(task.quantity) * req.consumption_per_unit))
        return rows

    if task.part_id and task.part.material and task.part.material.raw_material_id:
        material = task.part.material
        return [(material.raw_material, Decimal(task.quantity) * material.consumption_per_unit)]

    return rows


@login_required
@warehouse_or_manager_required
def production_issue_queue(request):
    """A warehouse-first queue: one line is one material required by one production task."""
    from product.models import ProductionTask

    if request.method == 'POST' and request.POST.get('action') == 'request':
        task = get_object_or_404(ProductionTask, pk=request.POST.get('task_id'))
        # P4: برای نقاشی عادی، مسیر عملیاتی واحد «صف مواد روزانه» است.
        # ساختن MaterialIssue برای تسک نقاشی یک مسیر موازی روی همان دفتر
        # StockMovement می‌ساخت و مصرف را دوبار کم می‌کرد.
        if task.station_name == 'paint':
            messages.error(
                request,
                'برای نقاشی، درخواست مواد از صف روزانهٔ انبار ثبت می‌شود؛ '
                'از صفحهٔ «صف مواد روزانه» برای همان تاریخ و کارگر اقدام کنید.',
            )
            return redirect('inventory:production_issue_queue')
        raw = get_object_or_404(RawMaterial, pk=request.POST.get('raw_material_id'))
        try:
            quantity = Decimal(request.POST.get('quantity', '0'))
        except InvalidOperation:
            messages.error(request, 'مقدار درخواست نامعتبر است.')
            return redirect('inventory:production_issue_queue')
        if quantity <= 0:
            messages.error(request, 'مقدار درخواست باید بزرگ‌تر از صفر باشد.')
        else:
            issue, created = MaterialIssue.objects.get_or_create(
                task=task, raw_material=raw, purpose='production', status='requested',
                defaults={'requested_quantity': quantity, 'requested_by': request.user,
                          'note': f'نیاز برنامه تولید: سفارش {task.order_id}'}
            )
            messages.success(request, 'درخواست تحویل مواد ثبت شد.' if created else 'این درخواست پیش‌تر در صف انبار ثبت شده است.')
        return redirect('inventory:production_issue_queue')

    station = request.GET.get('station', '')
    q = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '')

    # ---------- درخواست‌های انبار ----------
    issues_qs = MaterialIssue.objects.select_related(
        'raw_material', 'task__order', 'task__order_item__product',
        'order_item__order', 'order_item__product', 'painting_process',
        'defect__order', 'defect__order_item__product',
        'defect__packaging_unit__order_item__product', 'defect__task__painting_stage__process',
        'packaging_unit__order_item__product'
    ).order_by('-created_at')

    issues_qs = (
        issues_qs.filter(status=status_filter) if status_filter
        else issues_qs.filter(status__in=['requested', 'partial'])
    )
    issues_qs = issues_qs.filter(
        Q(purpose='production', task__isnull=False) |          # سایر ایستگاه‌ها (per-task)
        Q(purpose='production', task__isnull=True, order_item__isnull=False) |  # نقاشی (per-item)
        Q(purpose='rework', defect__isnull=False)
    )

    if station == 'paint':
        issues_qs = issues_qs.filter(
            Q(task__station_name='paint') |
            Q(task__isnull=True, order_item__isnull=False)
        )
    elif station:
        issues_qs = issues_qs.filter(task__station_name=station)

    if q:
        issues_qs = issues_qs.filter(
            Q(task__order__id__icontains=q) |
            Q(task__order_item__product__name__icontains=q) |
            Q(raw_material__name__icontains=q) |
            Q(defect__order__id__icontains=q) |
            Q(defect__order_item__product__name__icontains=q) |
            Q(defect__packaging_unit__order_item__product__name__icontains=q) |
            Q(defect__packaging_unit__unit_number__icontains=q) |
            Q(packaging_unit__order_item__product__name__icontains=q) |
            Q(packaging_unit__unit_number__icontains=q) |
            Q(defect__color_part__icontains=q) |
            Q(order_item__id__icontains=q) |
            Q(order_item__product__name__icontains=q) |
            Q(order_item__order__id__icontains=q)
        )

    paginator = Paginator(issues_qs, 25)
    issues = paginator.get_page(request.GET.get('page'))

    # مجموع نیازِ کل صف (نه فقط ۲۵ ردیف نمایش‌داده‌شده) برای نمای بالای جدول.
    aggregate_rows = services.aggregate_needs(issues_qs)

    # Warehouse users for handover receiver dropdown
    warehouse_users = User.objects.filter(is_active=True).order_by('first_name', 'last_name', 'username')

    custody_users = _open_custody_users()

    return _no_store(render(request, 'inventory/production_issue_queue.html', {
        **_inventory_context('production_queue'),
        'issues': issues,
        'issues_total': paginator.count,
        'aggregate_rows': aggregate_rows,
        'station': station,
        'stations': ProductionTask.STATION_CHOICES,
        'warehouse_users': warehouse_users,
        'paint_workers': _paint_workers(),
        'custody_users': custody_users,
        'page_version': _page_version(),
    }))


# ============================================================
# Group Handover API (New)
# ============================================================

CENT = Decimal('0.01')


def _json_body(request):
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _to_quantity(value):
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise HandoverError('مقدار یکی از ردیف‌ها نامعتبر است.')
    if not qty.is_finite() or qty <= 0:
        raise HandoverError('مقدار تحویل هر ردیف باید بزرگ‌تر از صفر باشد.')
    try:
        return qty.quantize(CENT)
    except InvalidOperation:
        raise HandoverError('مقدار یکی از ردیف‌ها نامعتبر است.')


def _parse_items(payload):
    raw_items = payload.get('items') if payload else None
    if not isinstance(raw_items, list) or not raw_items:
        raise HandoverError('هیچ موردی انتخاب نشده است.')

    items, seen = [], set()
    for row in raw_items:
        if not isinstance(row, dict):
            raise HandoverError('داده ارسالی نامعتبر است.')
        try:
            issue_id = int(row.get('issue_id'))
        except (TypeError, ValueError):
            raise HandoverError('شناسه یکی از درخواست‌ها نامعتبر است.')
        if issue_id in seen:
            raise HandoverError(f'درخواست #{issue_id} بیش از یک‌بار انتخاب شده است.')
        seen.add(issue_id)
        items.append((issue_id, _to_quantity(row.get('quantity'))))
    return items


def _parse_held_by(payload):
    """شناسهٔ کارگر امانت‌گیرنده را از payload می‌خواند و اعتبارسنجی می‌کند."""
    raw = payload.get('held_by') if payload else None
    if raw in (None, '', 'null'):
        return None
    try:
        held_id = int(raw)
    except (TypeError, ValueError):
        raise HandoverError('کارگر امانت‌گیرنده نامعتبر است.')
    holder = User.objects.filter(pk=held_id, is_active=True).first()
    if holder is None:
        raise HandoverError('کارگر امانت‌گیرنده را انتخاب کنید.')
    return holder


@login_required
@warehouse_or_manager_required
@require_POST
def handover_preview(request):
    """خلاصهٔ جمع مواد انتخاب‌شده + محاسبهٔ قوطی و باقی‌مانده (بدون ثبت)."""
    payload = _json_body(request)
    try:
        items = _parse_items(payload)
        held_by = _parse_held_by(payload)
        rows = services.preview_handover(items, held_by=held_by)
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)})
    return JsonResponse({'success': True, 'rows': rows})


@login_required
@warehouse_or_manager_required
@require_POST
def handover_create(request):
    """ثبت نهایی تحویل گروهی به یک تحویل‌گیرنده."""
    payload = _json_body(request)
    try:
        items = _parse_items(payload)

        try:
            receiver_id = int(payload.get('received_by'))
        except (TypeError, ValueError):
            receiver_id = None
        receiver = User.objects.filter(pk=receiver_id, is_active=True).first() if receiver_id else None
        if receiver is None:
            raise HandoverError('تحویل‌گیرنده را انتخاب کنید.')

        held_by = _parse_held_by(payload)

        handover = services.execute_handover(
            issued_by=request.user,
            received_by=receiver,
            items=items,
            note=str(payload.get('note') or '').strip()[:255],
            held_by=held_by,
        )
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)})

    receiver_name = receiver.get_full_name() or receiver.username
    if held_by is not None:
        holder_name = held_by.get_full_name() or held_by.username
        messages.success(
            request,
            f'تحویل {len(items)} ردیف به «{receiver_name}» ثبت شد (تحویل شماره {handover.id}). '
            f'باقی‌ماندهٔ بسته‌ها به‌عنوان امانت نزد «{holder_name}» ثبت شد.'
        )
    else:
        messages.success(
            request,
            f'تحویل {len(items)} ردیف به «{receiver_name}» ثبت شد (تحویل شماره {handover.id}).'
        )
    return JsonResponse({'success': True, 'handover_id': handover.id})


# ---------------------------------------------------------------------------
# تحویل مجموع (یک مقدار کل، پخش‌شده بین درخواست‌های بازِ همان ماده)
# ---------------------------------------------------------------------------

def _parse_aggregate_payload(payload):
    """خواندن و اعتبارسنجی payload تحویل مجموع."""
    if not isinstance(payload, dict):
        raise HandoverError('داده ارسالی نامعتبر است.')
    try:
        raw_id = int(payload.get('raw_material_id'))
    except (TypeError, ValueError):
        raise HandoverError('ماده اولیه انتخاب نشده است.')

    try:
        receiver_id = int(payload.get('received_by'))
    except (TypeError, ValueError):
        receiver_id = None
    receiver = User.objects.filter(pk=receiver_id, is_active=True).first() if receiver_id else None
    if receiver is None:
        raise HandoverError('تحویل‌گیرنده را انتخاب کنید.')

    held_by = _parse_held_by(payload)
    return raw_id, receiver, held_by


@login_required
@warehouse_or_manager_required
@require_POST
def aggregate_handover_preview(request):
    """پیش‌نمایش یک تحویل مجموعی، با نمایش اینکه چه مقدار به چه درخواستی می‌رسد."""
    payload = _json_body(request)
    try:
        raw_id, receiver, held_by = _parse_aggregate_payload(payload)
        items = services.distribute_aggregate(raw_id, payload.get('quantity'))
        rows = services.preview_handover(items, held_by=held_by)
        allocation = [
            {
                'issue_id': issue_id,
                'quantity': str(qty),
                'label': _issue_label(issue_id),
            }
            for issue_id, qty in items
        ]
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)})
    return JsonResponse({
        'success': True,
        'rows': rows,
        'allocation': allocation,
        'received_by': receiver.get_full_name() or receiver.username,
        'held_by': (held_by.get_full_name() or held_by.username) if held_by else '',
    })


def _issue_label(issue_id):
    """برچسب کوتاه یک درخواست برای نمایش در پیش‌نمایش توزیع."""
    issue = MaterialIssue.objects.select_related(
        'raw_material', 'task', 'order_item__product', 'defect__order'
    ).filter(pk=issue_id).first()
    if issue is None:
        return f'درخواست #{issue_id}'
    if issue.defect_id:
        return f'خرابی #{issue.defect_id} — سفارش {issue.defect.order_id}'
    if issue.task_id:
        return f'سفارش {issue.task.order_id} — {issue.task.get_station_name_display()}'
    if issue.order_item_id:
        product = getattr(issue.order_item, 'product', None)
        return f'آیتم #{issue.order_item_id}' + (f' — {product.name}' if product else '')
    return f'درخواست #{issue_id}'


@login_required
@warehouse_or_manager_required
@require_POST
def aggregate_handover_create(request):
    """ثبت نهایی تحویل مجموعی؛ مقدار کل تناسبی بین درخواست‌های باز پخش می‌شود."""
    payload = _json_body(request)
    try:
        raw_id, receiver, held_by = _parse_aggregate_payload(payload)
        items = services.distribute_aggregate(raw_id, payload.get('quantity'))
        handover = services.execute_handover(
            issued_by=request.user,
            received_by=receiver,
            items=items,
            note=str(payload.get('note') or '').strip()[:255],
            held_by=held_by,
        )
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)})

    receiver_name = receiver.get_full_name() or receiver.username
    message = (
        f'تحویل مجموعی {len(items)} درخواست به «{receiver_name}» ثبت شد '
        f'(تحویل شماره {handover.id}).'
    )
    if held_by is not None:
        message += f' باقی‌ماندهٔ بسته‌ها امانت «{held_by.get_full_name() or held_by.username}» شد.'
    messages.success(request, message)
    return JsonResponse({'success': True, 'handover_id': handover.id, 'rows': len(items)})


# ============================================================
# امانت مواد نزد کارگر (Material Custody)
# ============================================================

@login_required
@warehouse_or_manager_required
def custody_board(request):
    """صفحهٔ «تحویل روزانه نقاشی»: باقی‌ماندهٔ بسته‌های باز و ثبت بازگشت پایان روز."""
    worker_id = request.GET.get('worker', '').strip()
    search = request.GET.get('q', '').strip()
    only_open = request.GET.get('only_open', '') in ('1', 'on', 'true')

    worker = None
    if worker_id:
        try:
            worker_pk = int(worker_id)
        except (TypeError, ValueError):
            messages.error(request, 'کارگر انتخاب‌شده یافت نشد.')
            return redirect('inventory:custody_board')
        worker = User.objects.filter(pk=worker_pk, is_active=True).first()
        if worker is None:
            messages.error(request, 'کارگر انتخاب‌شده یافت نشد.')
            return redirect('inventory:custody_board')

    overview = services.custody_overview(held_by=worker, search=search, only_open=only_open)

    return _no_store(render(request, 'inventory/custody_board.html', {
        **_inventory_context('custody'),
        'rows': overview['rows'],
        'summary': overview['summary'],
        'paint_workers': _paint_workers(),
        'worker': worker,
        'search': search,
        'only_open': only_open,
        'page_version': _page_version(),
    }))


# ============================================================
# Daily Material Queue (Phase 4)
# ============================================================

@login_required
@warehouse_or_manager_required
def daily_material_queue(request):
    """صفحهٔ صف مواد روزانه انبار — مشتق از برنامهٔ نقاشی.

    گزارش‌گیری و جمع‌ها در ``services.daily_material_report`` است تا منطق
    مصرف یک تعریف داشته باشد؛ این ویو فقط فیلترها را می‌خواند و صفحه را
    رندر می‌کند.
    """
    import jdatetime
    from product.utils import parse_jalali_date

    # تاریخ انتخابی به تقویم شمسی (مثل بقیهٔ صفحات برنامه) — پیش‌فرض: امروز.
    # استفاده از parse_jalali_date عمداً مهم است: widget تاریخ در قالب، شمسی
    # ارسال می‌کند و تفسیر آن به‌عنوان میلادی روز اشتباه را نشان می‌داد.
    date_str = request.GET.get('date', '').strip()
    if date_str:
        try:
            selected_date = parse_jalali_date(date_str)
        except (ValueError, TypeError):
            selected_date = jdatetime.date.today()
    else:
        selected_date = jdatetime.date.today()

    def _int(raw):
        try:
            return int(raw) if raw not in (None, '') else None
        except (TypeError, ValueError):
            return None

    worker_filter = request.GET.get('worker', '').strip()
    material_filter = request.GET.get('material', '').strip()
    status_filter = request.GET.get('status', '').strip()

    report = services.daily_material_report(
        selected_date.togregorian(),
        worker_id=_int(worker_filter),
        material_id=_int(material_filter),
        status=status_filter or None,
    )

    paginator = Paginator(report['rows'], 25)
    queues = paginator.get_page(request.GET.get('page'))

    context = {
        **_inventory_context('daily_queue'),
        'queues': queues,
        'summary': report['summary'],
        'total_count': paginator.count,
        'selected_gregorian': selected_date.togregorian(),
        'date_str': selected_date.strftime('%Y-%m-%d'),
        'paint_workers': _paint_workers(),
        'materials': RawMaterial.objects.filter(is_active=True).order_by('category__name', 'name'),
        'status_choices': DailyMaterialQueue.STATUS_CHOICES,
        'worker_filter': worker_filter,
        'material_filter': material_filter,
        'status_filter': status_filter,
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/daily_material_queue.html', context))


@login_required
@warehouse_or_manager_required
@require_POST
def daily_queue_delivery(request, queue_id):
    """تحویل مواد برای یک ردیف صف روزانه.

    تمام منطق تحویل (مقدار بسته‌بندی، بررسی موجودی، ثبت حرکت انبار) در
    ``services.execute_daily_delivery`` است؛ این ویو فقط آن را صدا می‌زند و
    هیچ مقدار موجودی را مستقیم دستکاری نمی‌کند.
    """
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()

    try:
        queue = services.execute_daily_delivery(
            queue_id=queue_id,
            delivered_by=request.user,
            note=str(request.POST.get('note') or '').strip(),
        )
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception:
        # جزئیات خطا به کاربر نشان داده نمی‌شود؛ فقط ثبت می‌شود.
        logger.exception('daily_queue_delivery: unexpected error for queue %s', queue_id)
        return JsonResponse(
            {'success': False, 'error': 'ثبت تحویل انجام نشد. لطفاً دوباره تلاش کنید.'},
            status=500,
        )

    messages.success(
        request,
        f'تحویل مواد با موفقیت ثبت شد — «{queue.raw_material.name}» به '
        f'«{queue.worker.get_full_name() or queue.worker.username}».',
    )
    return JsonResponse({
        'success': True,
        'delivered_quantity': str(queue.delivered_quantity),
        'status': queue.status,
        'status_display': queue.get_status_display(),
        'actual_consumption': str(queue.actual_consumption),
    })


@login_required
@warehouse_or_manager_required
@require_POST
def daily_queue_return(request, queue_id):
    """ثبت بازگشت مواد برای یک ردیف صف روزانه."""
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()

    try:
        returned_quantity = request.POST.get('returned_quantity')
        if returned_quantity in (None, ''):
            return JsonResponse({'success': False, 'error': 'مقدار برگشتی را وارد کنید.'}, status=400)

        queue = services.execute_daily_return(
            queue_id=queue_id,
            returned_by=request.user,
            returned_quantity=returned_quantity,
            note=str(request.POST.get('note') or '').strip(),
        )
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception:
        logger.exception('daily_queue_return: unexpected error for queue %s', queue_id)
        return JsonResponse(
            {'success': False, 'error': 'ثبت بازگشت انجام نشد. لطفاً دوباره تلاش کنید.'},
            status=500,
        )

    messages.success(request, 'بازگشت مواد با موفقیت ثبت شد.')
    return JsonResponse({
        'success': True,
        'returned_quantity': str(queue.returned_quantity),
        'actual_consumption': str(queue.actual_consumption),
        'excess_consumption': str(queue.excess_consumption),
        'status': queue.status,
        'status_display': queue.get_status_display(),
    })


@login_required
@warehouse_or_manager_required
def daily_closing(request):
    """کنترل و بستن روز — فقط خواندنی (Phase 8).

    هیچ نوشتنی انجام نمی‌شود؛ فقط از سرویس‌های گزارش Phase 7 خوانده می‌شود.
    """
    import jdatetime
    from product.utils import parse_jalali_date

    date_str = request.GET.get('date', '').strip()
    if date_str:
        try:
            selected_date = parse_jalali_date(date_str)
        except (ValueError, TypeError):
            selected_date = jdatetime.date.today()
    else:
        selected_date = jdatetime.date.today()

    def _int(raw):
        try:
            return int(raw) if raw not in (None, '') else None
        except (TypeError, ValueError):
            return None

    worker_filter = request.GET.get('worker', '').strip()
    material_filter = request.GET.get('material', '').strip()

    control = services.daily_closing_status(
        selected_date.togregorian(),
        worker_id=_int(worker_filter),
        material_id=_int(material_filter),
    )
    closing = services.get_daily_closing(selected_date.togregorian())

    context = {
        **_inventory_context('daily_closing'),
        'control': control,
        'closing': closing,
        'already_closed': closing is not None,
        'summary': control['summary'],
        'problems': control['problems'],
        'selected_date': selected_date,
        'selected_gregorian': selected_date.togregorian(),
        'date_str': selected_date.strftime('%Y-%m-%d'),
        'paint_workers': _paint_workers(),
        'materials': RawMaterial.objects.filter(is_active=True).order_by('category__name', 'name'),
        'worker_filter': worker_filter,
        'material_filter': material_filter,
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/daily_closing.html', context))


@login_required
@warehouse_or_manager_required
@require_POST
def daily_closing_confirm(request):
    """ثبت «روز بررسی و تأیید شد» (Phase 9).

    هیچ StockMovement ایجاد نمی‌کند و هیچ مقدار Queue را تغییر نمی‌دهد؛
    فقط سند تأیید روز را ثبت می‌کند.
    """
    from . import reports  # noqa: F401  (بسته به مسیر استفاده می‌شود)
    import jdatetime
    from product.utils import parse_jalali_date

    date_str = request.POST.get('date', '').strip()
    try:
        selected_date = parse_jalali_date(date_str) if date_str else jdatetime.date.today()
    except (ValueError, TypeError):
        messages.error(request, 'تاریخ واردشده معتبر نیست.')
        return redirect('inventory:daily_closing')

    try:
        services.confirm_daily_closing(
            date=selected_date.togregorian(),
            closed_by=request.user,
            note=request.POST.get('note', ''),
        )
    except services.HandoverError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'روز {selected_date.strftime("%Y/%m/%d")} بررسی و تأیید شد.',
        )
    return redirect('inventory:daily_closing')


# ---------------------------------------------------------------------
#  Phase 10 — دفتر گردش مواد
# ---------------------------------------------------------------------

def _report_filters(request):
    """خواندن فیلترهای مشترک گزارش‌ها به‌صورت خوانا و بدون تکرار."""
    def _int(name):
        raw = request.GET.get(name, '').strip()
        try:
            return int(raw) if raw else None
        except (TypeError, ValueError):
            return None

    return {
        'date_from': _parse_date_param(request.GET.get('date_from')),
        'date_to': _parse_date_param(request.GET.get('date_to')),
        'material_id': _int('material'),
        'worker_id': _int('worker'),
        'order_id': _int('order'),
        'movement_type': request.GET.get('movement_type', '').strip() or None,
        'product_id': _int('product'),
        'color_part': request.GET.get('color_part', '').strip() or None,
        'status': request.GET.get('status', '').strip() or None,
    }


def _historical_kwargs(filters):
    """فقط کلیدهایی که ``historical_report`` می‌پذیرد (مثلاً movement_type ندارد)."""
    allowed = (
        'date_from', 'date_to', 'material_id', 'worker_id', 'order_id',
        'product_id', 'color_part', 'status',
    )
    return {key: filters[key] for key in allowed if key in filters}


def _parse_date_param(raw):
    """تاریخ میلادی از query string (YYYY-MM-DD)؛ در نبود مقدار، None."""
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return timezone.datetime.strptime(raw, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


@login_required
@warehouse_or_manager_required
def material_ledger(request):
    """دفتر گردش مواد بر اساس StockMovement — فقط خواندنی."""
    from . import reports

    filters = _report_filters(request)
    ledger = reports.material_ledger(**{
        k: v for k, v in filters.items()
        if k in ('date_from', 'date_to', 'material_id', 'worker_id',
                 'order_id', 'movement_type')
    })

    context = {
        **_inventory_context('material_ledger'),
        'ledger': ledger,
        'rows': ledger['rows'],
        'materials': ledger['materials'],
        'movement_types': StockMovement.MOVEMENT_TYPES,
        'paint_workers': _paint_workers(),
        'filters': filters,
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/material_ledger.html', context))


# ---------------------------------------------------------------------
#  Phase 11 — تحلیل مصرف
# ---------------------------------------------------------------------

@login_required
@warehouse_or_manager_required
def consumption_report(request):
    """Planned / Delivered / Returned / Actual / Variance — فقط خواندنی."""
    from . import reports

    filters = _report_filters(request)
    report = reports.consumption_report(**{
        k: v for k, v in filters.items()
        if k in ('date_from', 'date_to', 'material_id', 'worker_id')
    })

    context = {
        **_inventory_context('consumption_report'),
        'report': report,
        'rows': report['rows'],
        'totals': report['totals'],
        'materials': RawMaterial.objects.filter(is_active=True).order_by('name'),
        'paint_workers': _paint_workers(),
        'filters': filters,
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/consumption_report.html', context))


# ---------------------------------------------------------------------
#  Phase 12 — ردیابی مواد سفارش
# ---------------------------------------------------------------------

@login_required
@warehouse_or_manager_required
def order_material_traceability(request, order_id):
    """مسیر کامل ماده از سفارش تا مصرف — فقط خواندنی."""
    from django.shortcuts import get_object_or_404 as _get
    from product.models import Order
    from . import reports

    order = _get(Order.objects.all(), pk=order_id)
    trace = reports.order_material_traceability(order)

    context = {
        **_inventory_context('orders'),
        'order': order,
        'trace': trace,
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/order_material_trace.html', context))


# ---------------------------------------------------------------------
#  Phase 13 — داشبورد برنامه‌ریزی مواد
# ---------------------------------------------------------------------

@login_required
@warehouse_or_manager_required
def material_dashboard(request):
    """داشبورد مدیریتی مواد + بخش انبار — فقط خواندنی."""
    from . import reports
    import jdatetime
    from product.utils import parse_jalali_date

    date_str = request.GET.get('date', '').strip()
    if date_str:
        try:
            selected = parse_jalali_date(date_str)
        except (ValueError, TypeError):
            selected = jdatetime.date.today()
    else:
        selected = jdatetime.date.today()

    data = reports.material_planning_dashboard(selected.togregorian())

    context = {
        **_inventory_context('material_dashboard'),
        'data': data,
        'manager': data['manager'],
        'warehouse': data['warehouse'],
        'date_str': selected.strftime('%Y-%m-%d'),
        'selected_date': selected,
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/material_dashboard.html', context))


# ---------------------------------------------------------------------
#  Phase 14 — هشدارها
# ---------------------------------------------------------------------

@login_required
@warehouse_or_manager_required
def inventory_alerts(request):
    """هشدارهای عملیاتی — فقط نمایش، بدون هیچ تراکنشی."""
    from . import reports
    import jdatetime
    from product.utils import parse_jalali_date

    date_str = request.GET.get('date', '').strip()
    if date_str:
        try:
            selected = parse_jalali_date(date_str)
        except (ValueError, TypeError):
            selected = jdatetime.date.today()
    else:
        selected = jdatetime.date.today()

    data = reports.inventory_alerts(selected.togregorian())

    context = {
        **_inventory_context('inventory_alerts'),
        'alerts': data['alerts'],
        'total': data['total'],
        'date_str': selected.strftime('%Y-%m-%d'),
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/inventory_alerts.html', context))


# ---------------------------------------------------------------------
#  Phase 15 — گزارش‌های تاریخی
# ---------------------------------------------------------------------

@login_required
@warehouse_or_manager_required
def historical_reports(request):
    """گزارش‌های تاریخی فقط‌خواندنی با خروجی HTML و Print."""
    from . import reports

    report_key = request.GET.get('report', 'consumption')
    filters = _report_filters(request)
    try:
        data = reports.historical_report(report=report_key, **_historical_kwargs(filters))
    except ValueError:
        data = None

    from product.models import Order, Product

    context = {
        **_inventory_context('historical_reports'),
        'data': data,
        'report_options': reports.HISTORICAL_REPORTS,
        'selected_report': report_key,
        'materials': RawMaterial.objects.filter(is_active=True).order_by('name'),
        'paint_workers': _paint_workers(),
        'orders': Order.objects.all().order_by('-id')[:200],
        'products': Product.objects.filter(is_active=True).order_by('name')[:200],
        'status_choices': DailyMaterialQueue.STATUS_CHOICES,
        'filters': filters,
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/historical_reports.html', context))


@login_required
@warehouse_or_manager_required
def historical_reports_csv(request):
    """خروجی CSV همان گزارش تاریخی (بدون وابستگی خارجی)."""
    import csv
    from . import reports

    report_key = request.GET.get('report', 'consumption')
    try:
        data = reports.historical_report(
            report=report_key, **_historical_kwargs(_report_filters(request)))
    except ValueError:
        return HttpResponse('گزارش ناشناخته', status=400, content_type='text/plain; charset=utf-8')

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    filename = f'craftflow-{report_key}.csv'
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    # BOM تا Excel فارسی را درست باز کند
    response.write('﻿')

    writer = csv.writer(response)
    writer.writerow(data['columns'])
    for row in data['rows']:
        writer.writerow(row)
    return response


# ---------------------------------------------------------------------
#  Phase 16 — حسابرسی یکپارچگی داده
# ---------------------------------------------------------------------

@login_required
@admin_or_manager_required
def data_integrity_audit(request):
    """تشخیص مشکلات یکپارچگی داده — فقط گزارش، بدون اصلاح خودکار."""
    from . import reports

    data = reports.data_integrity_audit()

    context = {
        **_inventory_context('audit'),
        'data': data,
        'issues': data['issues'],
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/data_integrity_audit.html', context))


@login_required
@warehouse_or_manager_required
def daily_queue_sources(request, queue_id):
    """منابع یک ردیف صف روزانه (برای تفکیک برنامه‌ریزی)."""
    queue = get_object_or_404(DailyMaterialQueue.objects.select_related('worker', 'raw_material'), pk=queue_id)
    sources = queue.sources.select_related('production_task__order_item__product', 'painting_stage__process').all()
    state = services.daily_queue_action_state(queue)

    rows = []
    for src in sources:
        rows.append({
            'task_id': src.production_task_id,
            'task_label': f"تسک #{src.production_task_id}",
            'stage_name': src.painting_stage.name if src.painting_stage else '—',
            'process_name': src.painting_stage.process.name if src.painting_stage and src.painting_stage.process else '—',
            'color_part': src.production_task.color_part or '—',
            'quantity': str(src.quantity),
            'order_item': f"آیتم #{src.production_task.order_item_id}" if src.production_task.order_item_id else '—',
        })

    return JsonResponse({
        'success': True,
        'queue_id': queue.id,
        'worker': queue.worker.get_full_name() or queue.worker.username,
        'material': queue.raw_material.name,
        'unit': queue.raw_material.get_unit_display(),
        'planned_quantity': str(queue.planned_quantity),
        'delivered_quantity': str(queue.delivered_quantity),
        'returned_quantity': str(queue.returned_quantity),
        'actual_consumption': str(queue.actual_consumption),
        'excess_consumption': str(queue.excess_consumption),
        'status': queue.status,
        'status_display': queue.get_status_display(),
        'has_plan_conflict': queue.has_plan_conflict,
        'conflict_note': queue.conflict_note or '',
        # وضعیت اقدام‌ها از سرویس می‌آید تا JS قاعده را تکرار نکند.
        'can_deliver': state['can_deliver'],
        'can_return': state['can_return'],
        'max_returnable': state['max_returnable'],
        'rows': rows,
    })


@login_required
@warehouse_or_manager_required
def daily_queue_preview_delivery(request, queue_id):
    """پیش‌نمایش تحویل برای یک ردیف صف روزانه."""
    try:
        preview = services.preview_daily_delivery(queue_id)
        return JsonResponse({
            'success': True,
            'planned': str(preview['planned']),
            'pack_size': str(preview['pack_size']),
            'packs': preview['packs'],
            'physical': str(preview['physical']),
            'stock': str(preview['stock']),
            'enough': preview['enough'],
            'name': preview['name'],
            'unit': preview['unit'],
            'status': preview['status'],
            'status_display': preview['status_display'],
            'can_deliver': preview['can_deliver'],
        })
    except services.HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


def _post_data(request):
    """
    داده‌های یک POST را می‌خواند: چه فرم‌-encoded باشد چه JSON.
    مرورگر فقط وقتی request.POST را پر می‌کند که Content-Type درست باشد،
    پس هر دو حالت پشتیبانی می‌شود.
    """
    if request.POST:
        return request.POST
    payload = _json_body(request) or {}
    return {
        key: ('' if value is None else value)
        for key, value in payload.items()
    }


def _parse_attributions(raw_value):
    """
    انتساب مصرف: فهرست ``[(defect, quantity)]`` از JSON یا فیلد فرم.

    قالب JSON هر عضو: ``{"defect_id": 12, "quantity": "0.50"}``.
    ردیف‌های ناقص یا بی‌مقدار نادیده گرفته می‌شوند تا ثبت بازگشت را بی‌دلیل
    نشکند؛ اعتبارسنجی اصلی در سرویس انجام می‌شود.
    """
    from product.models import ProductionDefect

    if raw_value in (None, '', [], ()):
        return []

    entries = []
    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value)
        except (ValueError, TypeError):
            raise HandoverError('فهرست انتساب مصرف نامعتبر است.')
    if not isinstance(raw_value, list):
        raise HandoverError('فهرست انتساب مصرف نامعتبر است.')

    for row in raw_value:
        if not isinstance(row, dict):
            continue
        try:
            defect_id = int(row.get('defect_id'))
        except (TypeError, ValueError):
            continue
        qty = row.get('quantity')
        if qty in (None, ''):
            continue
        defect = ProductionDefect.objects.filter(pk=defect_id).first()
        if defect is None:
            raise HandoverError(f'خرابی شماره {defect_id} یافت نشد.')
        entries.append((defect, qty))
    return entries


@login_required
@warehouse_or_manager_required
@require_POST
def custody_return(request):
    """ثبت بازگشت پایان روز: مقدار واقعی توزین‌شدهٔ باقیماندهٔ نزد کارگر."""
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()

    data = _post_data(request)

    try:
        raw_id = int(data.get('raw_material_id'))
        held_id = int(data.get('held_by'))
    except (TypeError, ValueError):
        return JsonResponse(
            {'success': False, 'error': 'ماده اولیه یا کارگر به‌درستی انتخاب نشده است.'},
            status=400,
        )

    raw = RawMaterial.objects.filter(pk=raw_id).first()
    if raw is None:
        return JsonResponse({'success': False, 'error': 'ماده اولیه یافت نشد.'}, status=400)
    holder = User.objects.filter(pk=held_id, is_active=True).first()
    if holder is None:
        return JsonResponse({'success': False, 'error': 'کارگر تحویل‌گیرنده یافت نشد.'}, status=400)

    try:
        attributions = _parse_attributions(data.get('attributions'))
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    try:
        result = services.return_custody(
            raw_material=raw,
            held_by=holder,
            measured_quantity=data.get('measured_quantity'),
            recorded_by=request.user,
            note=str(data.get('note') or '').strip(),
            to_warehouse=data.get('to_warehouse'),
            attributions=attributions,
        )
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    record = result['record']
    holder_name = holder.get_full_name() or holder.username
    return JsonResponse({
        'success': True,
        'material': raw.name,
        'quantity': str(record.measured_quantity),
        'delta': str(record.delta),
        'held_by': holder_name,
        'consumed': str(result['consumed']),
        'warehouse_return': str(result['warehouse_return']),
        'attributed': str(result['attributed_total']),
        'shortfall': str(result['shortfall']),
        'attributions': [
            {'defect_id': row.defect_id, 'quantity': str(row.quantity)}
            for row in result['attributions']
        ],
    })


def _defect_option(defect):
    """گزینهٔ قابل انتخاب برای انتساب مصرف در UI."""
    parts = [f'خرابی #{defect.pk}']
    if defect.order_id:
        parts.append(f'سفارش {defect.order_id}')
    product = getattr(defect.order_item, 'product', None)
    if product:
        parts.append(product.name)
    if defect.color_part:
        parts.append(defect.get_color_part_display())
    return {
        'id': defect.pk,
        'label': ' — '.join(parts),
        'status': defect.status,
        'description': (defect.description or '')[:120],
    }


@login_required
@warehouse_or_manager_required
@require_POST
def defect_choices(request):
    """فهرست خرابی‌های باز برای انتساب مصرفِ پایان روز."""
    data = _post_data(request)
    raw_id = data.get('raw_material_id')
    if raw_id in (None, ''):
        return JsonResponse(
            {'success': False, 'error': 'ماده اولیه نامعتبر است.'}, status=400
        )
    try:
        raw = RawMaterial.objects.filter(pk=int(raw_id)).first()
    except (TypeError, ValueError):
        raw = None
    if raw is None:
        return JsonResponse(
            {'success': False, 'error': 'ماده اولیه یافت نشد.'}, status=400
        )
    try:
        limit = int(data.get('limit') or 50)
    except (TypeError, ValueError):
        limit = 50
    defects = services.open_defect_choices(
        search=str(data.get('q') or '').strip(),
        raw_material=raw,
        limit=max(1, min(limit, 200)),
    )
    return JsonResponse({
        'success': True,
        'options': [_defect_option(d) for d in defects],
    })


# ============================================================
# Legacy Single-Issue Hand-over (kept for backward compat)
# ============================================================

@login_required
@warehouse_or_manager_required
@require_http_methods(['POST'])
def issue_material(request, issue_id):
    """Confirm hand-over for a single queue row through the shared handover service."""
    try:
        quantity = _to_quantity(request.POST.get('quantity'))
        holder = None
        raw_held = (request.POST.get('held_by') or '').strip()
        if raw_held:
            try:
                holder = User.objects.filter(pk=int(raw_held), is_active=True).first()
            except (TypeError, ValueError):
                holder = None
        services.execute_handover(
            issued_by=request.user,
            received_by=holder or request.user,
            items=[(issue_id, quantity)],
            note='تحویل تک‌ردیف',
            held_by=holder,
        )
        if holder is not None:
            holder_name = holder.get_full_name() or holder.username
            messages.success(
                request,
                f'تحویل مواد و خروج انبار ثبت شد. باقی‌ماندهٔ بسته‌ها به‌عنوان امانت نزد «{holder_name}» ثبت شد.',
            )
        else:
            messages.success(request, 'تحویل مواد و خروج انبار ثبت شد.')
    except HandoverError as exc:
        messages.error(request, str(exc))
    return redirect('inventory:production_issue_queue')


@login_required
@warehouse_or_manager_required
@require_http_methods(['POST'])
def cancel_material_issue(request, issue_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()

    with atomic():
        issue = get_object_or_404(
            MaterialIssue.objects.select_for_update().select_related('defect', 'packaging_unit'),
            pk=issue_id
        )
        if issue.status not in ('requested', 'partial'):
            return JsonResponse({'success': False, 'error': 'این درخواست دیگر قابل لغو نیست.'})
        issue.status = 'cancelled'
        issue.save(update_fields=['status'])
        if issue.defect_id and issue.defect.status == 'material_requested':
            issue.defect.status = 'reported'
            issue.defect.save(update_fields=['status'])
    return JsonResponse({'success': True})


# ============================================================
# Suppliers
# ============================================================

@login_required
@admin_or_manager_required
def supplier_list(request):
    search = request.GET.get('search', '')
    suppliers = Supplier.objects.all()
    if search:
        suppliers = suppliers.filter(Q(name__icontains=search) | Q(phone__icontains=search))
    suppliers = suppliers.order_by('name')

    paginator = Paginator(suppliers, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        table_html = render_to_string('inventory/_supplier_rows.html', {
            'suppliers': page_obj,
        })
        return JsonResponse({
            'success': True,
            'table_html': table_html,
            'total': paginator.count,
            'page': page_obj.number,
            'total_pages': paginator.num_pages,
        })

    context = {
        **_inventory_context('suppliers'),
        'suppliers': page_obj,
        'search': search,
        'form': SupplierForm(),
    }
    return render(request, 'inventory/suppliers.html', context)


@login_required
@admin_or_manager_required
@require_http_methods(['GET'])
def supplier_detail_api(request, supplier_id):
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    return JsonResponse({
        'id': supplier.id,
        'name': supplier.name,
        'phone': supplier.phone,
        'address': supplier.address,
        'is_active': supplier.is_active,
    })


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def supplier_create(request):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    form = SupplierForm(request.POST)
    if form.is_valid():
        supplier = form.save()
        return JsonResponse({'success': True, 'id': supplier.id, 'name': supplier.name})
    return JsonResponse({'success': False, 'errors': form.errors})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def supplier_edit(request, supplier_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    form = SupplierForm(request.POST, instance=supplier)
    if form.is_valid():
        form.save()
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'errors': form.errors})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def supplier_delete(request, supplier_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    try:
        supplier.delete()
        return JsonResponse({'success': True})
    except ProtectedError:
        return JsonResponse({'success': False, 'error': 'این تامین‌کننده دارای سفارش خرید است و نمی‌تواند حذف شود.'})


# ============================================================
# Categories
# ============================================================

@login_required
@admin_or_manager_required
def category_list(request):
    search = request.GET.get('search', '')
    categories = RawMaterialCategory.objects.all()
    if search:
        categories = categories.filter(name__icontains=search)
    categories = categories.order_by('name')

    paginator = Paginator(categories, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        table_html = render_to_string('inventory/_category_rows.html', {
            'categories': page_obj,
        })
        return JsonResponse({
            'success': True,
            'table_html': table_html,
            'total': paginator.count,
            'page': page_obj.number,
            'total_pages': paginator.num_pages,
        })

    context = {
        **_inventory_context('categories'),
        'categories': page_obj,
        'search': search,
        'form': RawMaterialCategoryForm(),
    }
    return render(request, 'inventory/categories.html', context)


@login_required
@admin_or_manager_required
@require_http_methods(['GET'])
def category_detail_api(request, category_id):
    category = get_object_or_404(RawMaterialCategory, pk=category_id)
    return JsonResponse({'id': category.id, 'name': category.name})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def category_create(request):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    form = RawMaterialCategoryForm(request.POST)
    if form.is_valid():
        form.save()
        return JsonResponse({'success': True, 'id': form.instance.id, 'name': form.instance.name})
    return JsonResponse({'success': False, 'errors': form.errors})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def category_edit(request, category_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    category = get_object_or_404(RawMaterialCategory, pk=category_id)
    form = RawMaterialCategoryForm(request.POST, instance=category)
    if form.is_valid():
        form.save()
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'errors': form.errors})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def category_delete(request, category_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    category = get_object_or_404(RawMaterialCategory, pk=category_id)
    try:
        category.delete()
        return JsonResponse({'success': True})
    except ProtectedError:
        return JsonResponse({'success': False, 'error': 'این دسته دارای مواد اولیه است و نمی‌تواند حذف شود.'})


# ============================================================
# Raw Materials
# ============================================================

@login_required
@admin_or_manager_required
def raw_material_list(request):
    search = request.GET.get('search', '')
    category_id = request.GET.get('category', '')
    materials = RawMaterial.objects.select_related('category').all()
    if search:
        materials = materials.filter(Q(name__icontains=search) | Q(code__icontains=search))
    if category_id:
        materials = materials.filter(category_id=category_id)
    materials = materials.order_by('category__name', 'name')

    paginator = Paginator(materials, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    categories = RawMaterialCategory.objects.all().order_by('name')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        table_html = render_to_string('inventory/_raw_material_rows.html', {
            'materials': page_obj,
        })
        return JsonResponse({
            'success': True,
            'table_html': table_html,
            'total': paginator.count,
            'page': page_obj.number,
            'total_pages': paginator.num_pages,
        })

    context = {
        **_inventory_context('materials'),
        'materials': page_obj,
        'categories': categories,
        'search': search,
        'category_filter': category_id,
        'form': RawMaterialForm(),
    }
    return render(request, 'inventory/materials.html', context)


@login_required
@admin_or_manager_required
@require_http_methods(['GET'])
def raw_material_detail_api(request, material_id):
    material = get_object_or_404(RawMaterial, pk=material_id)
    return JsonResponse({
        'id': material.id,
        'name': material.name,
        'code': material.code,
        'barcode': material.barcode,
        'category': material.category_id,
        'unit': material.unit,
        'min_stock_alert': str(material.min_stock_alert),
        'pack_size': str(material.pack_size),
        'is_active': material.is_active,
    })


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def raw_material_create(request):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    form = RawMaterialForm(request.POST)
    if form.is_valid():
        form.save()
        return JsonResponse({'success': True, 'id': form.instance.id, 'name': form.instance.name})
    return JsonResponse({'success': False, 'errors': form.errors})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def raw_material_edit(request, material_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    material = get_object_or_404(RawMaterial, pk=material_id)
    form = RawMaterialForm(request.POST, instance=material)
    if form.is_valid():
        form.save()
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'errors': form.errors})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def raw_material_delete(request, material_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    material = get_object_or_404(RawMaterial, pk=material_id)
    try:
        material.delete()
        return JsonResponse({'success': True})
    except ProtectedError:
        return JsonResponse({'success': False, 'error': 'این ماده اولیه دارای حرکات انبار یا سفارش خرید است و نمی‌تواند حذف شود.'})


# ============================================================
# Stock Movements
# ============================================================

@login_required
@admin_or_manager_required
def stock_movement_list(request):
    movements = StockMovement.objects.select_related(
        'raw_material', 'supplier', 'created_by'
    ).all().order_by('-created_at')

    search = request.GET.get('search', '')
    material_id = request.GET.get('material', '')
    movement_type = request.GET.get('type', '')

    if search:
        movements = movements.filter(
            Q(raw_material__name__icontains=search) |
            Q(note__icontains=search)
        )
    if material_id:
        movements = movements.filter(raw_material_id=material_id)
    if movement_type:
        movements = movements.filter(movement_type=movement_type)

    paginator = Paginator(movements, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    materials = RawMaterial.objects.filter(is_active=True).order_by('name')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        table_html = render_to_string('inventory/_stock_movement_rows.html', {
            'movements': page_obj,
        })
        return JsonResponse({
            'success': True,
            'table_html': table_html,
            'total': paginator.count,
            'page': page_obj.number,
            'total_pages': paginator.num_pages,
        })

    context = {
        **_inventory_context('movements'),
        'movements': page_obj,
        'materials': materials,
        'suppliers': Supplier.objects.filter(is_active=True).order_by('name'),
        'search': search,
        'material_filter': material_id,
        'type_filter': movement_type,
        'form': StockMovementForm(),
    }
    return render(request, 'inventory/movements.html', context)


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def stock_movement_create(request):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    form = StockMovementForm(request.POST)
    if form.is_valid():
        movement = form.save(commit=False)
        movement.created_by = request.user
        movement.save()
        return JsonResponse({
            'success': True,
            'id': movement.id,
            'material_name': movement.raw_material.name,
            'quantity': str(movement.quantity),
            'type_display': movement.get_movement_type_display(),
        })
    return JsonResponse({'success': False, 'errors': form.errors})


# ============================================================
# Purchase Orders
# ============================================================

@login_required
@admin_or_manager_required
def purchase_order_list(request):
    orders = PurchaseOrder.objects.select_related('supplier', 'created_by').all().order_by('-created_at')

    search = request.GET.get('search', '')
    status = request.GET.get('status', '')

    if search:
        orders = orders.filter(Q(supplier__name__icontains=search) | Q(note__icontains=search))
    if status:
        orders = orders.filter(status=status)

    paginator = Paginator(orders, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        table_html = render_to_string('inventory/_purchase_order_rows.html', {
            'orders': page_obj,
        })
        return JsonResponse({
            'success': True,
            'table_html': table_html,
            'total': paginator.count,
            'page': page_obj.number,
            'total_pages': paginator.num_pages,
        })

    context = {
        **_inventory_context('purchase_orders'),
        'orders': page_obj,
        'search': search,
        'status_filter': status,
        'form': PurchaseOrderForm(),
        'suppliers': Supplier.objects.filter(is_active=True).order_by('name'),
        'materials': RawMaterial.objects.filter(is_active=True).order_by('category__name', 'name'),
        'can_receive': orders.filter(status__in=['draft', 'ordered']).exists(),
    }
    return render(request, 'inventory/purchase_orders.html', context)


@login_required
@admin_or_manager_required
@require_http_methods(['GET'])
def purchase_order_detail_api(request, order_id):
    order = get_object_or_404(PurchaseOrder, pk=order_id)
    items = []
    for item in order.items.select_related('raw_material').all():
        items.append({
            'id': item.id,
            'raw_material_id': item.raw_material_id,
            'raw_material_name': item.raw_material.name,
            'quantity': str(item.quantity),
            'unit_price': str(item.unit_price),
            'received_quantity': str(item.received_quantity),
        })
    return JsonResponse({
        'id': order.id,
        'supplier': order.supplier_id,
        'supplier_name': order.supplier.name,
        'status': order.status,
        'note': order.note or '',
        'items': items,
    })


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def purchase_order_create(request):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    form = PurchaseOrderForm(request.POST)
    if form.is_valid():
        po = form.save(commit=False)
        po.created_by = request.user
        po.save()
        return JsonResponse({'success': True, 'id': po.id, 'supplier_name': po.supplier.name})
    return JsonResponse({'success': False, 'errors': form.errors})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def purchase_order_edit(request, order_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    po = get_object_or_404(PurchaseOrder, pk=order_id)
    form = PurchaseOrderForm(request.POST, instance=po)
    if form.is_valid():
        form.save()
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'errors': form.errors})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def purchase_order_delete(request, order_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    po = get_object_or_404(PurchaseOrder, pk=order_id)
    if po.status == 'received':
        return JsonResponse(
            {'success': False, 'error': 'سفارش دریافت‌شده قابل حذف نیست.'},
            status=400,
        )
    po.delete()
    return JsonResponse({'success': True})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def purchase_order_receive(request, order_id):
    """تحویل کالای سفارش خرید و ایجاد StockMovement ورودی."""
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    with atomic():
        po = get_object_or_404(PurchaseOrder.objects.select_for_update(), pk=order_id)
        if po.status == 'received':
            return JsonResponse({'success': False, 'error': 'این سفارش قبلاً دریافت شده است.'})

        total_received = Decimal('0')
        for item in po.items.all():
            qty = item.quantity - item.received_quantity
            if qty > 0:
                StockMovement.objects.create(
                    raw_material=item.raw_material,
                    movement_type='purchase',
                    quantity=qty,
                    unit_price=item.unit_price,
                    supplier=po.supplier,
                    note=f'فاکتور خرید PO-{po.id}',
                    created_by=request.user,
                )
                item.received_quantity = item.quantity
                item.save(update_fields=['received_quantity'])
                total_received += qty

        po.status = 'received'
        po.save(update_fields=['status'])

    return JsonResponse({'success': True, 'total_received': str(total_received)})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def purchase_order_item_add(request, order_id):
    """افزودن آیتم به سفارش خرید."""
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    po = get_object_or_404(PurchaseOrder, pk=order_id)
    if po.status == 'received':
        return JsonResponse(
            {'success': False, 'error': 'سفارش دریافت‌شده قابل تغییر نیست.'},
            status=400,
        )
    form = PurchaseOrderItemForm(request.POST)
    if form.is_valid():
        item = form.save(commit=False)
        item.purchase_order = po
        item.save()
        return JsonResponse({
            'success': True,
            'id': item.id,
            'raw_material_name': item.raw_material.name,
            'quantity': str(item.quantity),
            'unit_price': str(item.unit_price),
        })
    return JsonResponse({'success': False, 'errors': form.errors})


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def purchase_order_item_delete(request, item_id):
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()
    item = get_object_or_404(PurchaseOrderItem, pk=item_id)
    po = item.purchase_order
    item.delete()
    return JsonResponse({'success': True, 'order_id': po.id})


# ============================================================
# Low Stock Report
# ============================================================

@login_required
@admin_or_manager_required
def low_stock_report(request):
    materials = RawMaterial.objects.filter(is_active=True).annotate(
        stock=Coalesce(
            Sum(Case(
                When(movements__movement_type='consumption', then=-F('movements__quantity')),
                default=F('movements__quantity'),
                output_field=DecimalField()
            )),
            Value(0, output_field=DecimalField())
        )
    ).filter(stock__lte=F('min_stock_alert')).order_by('stock')

    context = {
        **_inventory_context('low_stock'),
        'materials': materials,
    }
    return render(request, 'inventory/low_stock.html', context)


# ============================================================
# Raw Material Barcode Scan for Receiving
# ============================================================

@login_required
@warehouse_or_manager_required
def raw_material_receive_scan(request):
    """صفحه اسکن بارکد برای دریافت مواد اولیه خریداری شده - ساده و سریع
    
    با اسکن بارکد، مواد اولیه به انبار اضافه می‌شود.
    مقدار به صورت خودکار از pack_size materiais خوانده می‌شود.
    نیاز به پر کردن فیلدهای قیمت و تامین‌کننده نیست.
    """
    if request.method == 'POST':
        barcode = request.POST.get('barcode', '').strip()
        try:
            pack_count = int(request.POST.get('pack_count', 1))
        except (TypeError, ValueError):
            pack_count = 0
        if pack_count < 1 or pack_count > 1000:
            messages.error(request, 'تعداد بسته باید بین ۱ تا ۱۰۰۰ باشد.')
            return redirect('inventory:raw_material_receive_scan')
        note = request.POST.get('note', '')

        if not barcode:
            messages.error(request, 'بارکد وارد نشده است.')
            return redirect('inventory:raw_material_receive_scan')

        try:
            raw_material = RawMaterial.objects.get(barcode=barcode)
        except RawMaterial.DoesNotExist:
            messages.error(request, f'ماده اولیه با بارکد «{barcode}» یافت نشد.')
            return redirect('inventory:raw_material_receive_scan')

        # محاسبه مقدار بر اساس تعداد بسته و pack_size
        if raw_material.pack_size and raw_material.pack_size > 0:
            quantity = Decimal(str(pack_count)) * raw_material.pack_size
        else:
            # اگر pack_size تعریف نشده باشد، 1 واحد در نظر گرفته می‌شود
            quantity = Decimal(str(pack_count))

        # ایجاد moviment ورودی انبار (بدون نیاز به سفارش خرید)
        StockMovement.objects.create(
            raw_material=raw_material,
            movement_type='purchase',
            quantity=quantity,
            unit_price=None,  # قیمت الزامی نیست
            supplier=None,    # تامین‌کننده الزامی نیست
            note=note or f'دریافت با اسکن بارکد - {pack_count} بسته',
            created_by=request.user,
        )

        messages.success(
            request,
            f'ماده «{raw_material.name}» با {pack_count} بسته ({quantity} {raw_material.get_unit_display()}) به انبار اضافه شد.'
        )
        return redirect('inventory:raw_material_receive_scan')

    # GET request - نمایش فرم اسکن
    recent_movements = StockMovement.objects.filter(
        movement_type='purchase'
    ).select_related('raw_material').order_by('-created_at')[:10]

    context = {
        **_inventory_context('raw_material_receive_scan'),
        'recent_movements': recent_movements,
    }
    return render(request, 'inventory/raw_material_receive_scan.html', context)


@login_required
@warehouse_or_manager_required
@require_http_methods(['POST'])
def raw_material_receive_scan_api(request):
    """API برای اسکن بارکد و دریافت سریع مواد اولیه"""
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return HttpResponseForbidden()

    barcode = request.POST.get('barcode', '').strip()
    try:
        pack_count = int(request.POST.get('pack_count', 1))
    except (TypeError, ValueError):
        pack_count = 0
    if pack_count < 1 or pack_count > 1000:
        return JsonResponse({'success': False, 'error': 'تعداد بسته باید بین ۱ تا ۱۰۰۰ باشد.'}, status=400)

    if not barcode:
        return JsonResponse({'success': False, 'error': 'بارکد وارد نشده است.'})

    try:
        raw_material = RawMaterial.objects.get(barcode=barcode)
    except RawMaterial.DoesNotExist:
        return JsonResponse({'success': False, 'error': f'ماده اولیه با بارکد «{barcode}» یافت نشد.'})

    # محاسبه مقدار بر اساس تعداد بسته و pack_size
    if raw_material.pack_size and raw_material.pack_size > 0:
        quantity = Decimal(str(pack_count)) * raw_material.pack_size
    else:
        quantity = Decimal(str(pack_count))

    # ایجاد moviment ورودی انبار
    movement = StockMovement.objects.create(
        raw_material=raw_material,
        movement_type='purchase',
        quantity=quantity,
        unit_price=None,
        supplier=None,
        note=f'دریافت با اسکن بارکد - {pack_count} بسته',
        created_by=request.user,
    )

    return JsonResponse({
        'success': True,
        'movement_id': movement.id,
        'material_name': raw_material.name,
        'material_unit': raw_material.get_unit_display(),
        'pack_count': pack_count,
        'quantity': str(quantity),
        'pack_size': str(raw_material.pack_size) if raw_material.pack_size else '0',
        'current_stock': str(raw_material.current_stock),
        'message': f'{raw_material.name} - {pack_count} بسته ({quantity} {raw_material.get_unit_display()})'
    })