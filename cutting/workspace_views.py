"""JSON API for the cutting workspace: order queue → items → editable parts.

Every endpoint is operator-gated, returns UTF-8 JSON with Persian messages,
and never writes to ``Part``/``ProductionTask`` base data — all edits land in
``TaskSpecOverride`` through ``cutting.specs``. Optimistic concurrency: the
client echoes back the row ``version``; a mismatch is HTTP 409 with the fresh
row so the UI can reload it.
"""
import json

import jdatetime
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Greatest
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from product.models import Order, ProductionTask

from .models import TaskSpecOverride
from .permissions import can_operate
from .specs import (
    STATUS_LABELS, SpecLockedError, apply_override, effective_spec,
    effective_specs, reset_override, set_excluded, workspace_task_queryset,
)
from .views import cutting_operator


def _json_body(request):
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError('بدنهٔ درخواست JSON معتبر نیست.')
    if not isinstance(data, dict):
        raise ValueError('بدنهٔ درخواست JSON معتبر نیست.')
    return data


def _error(message, status=400, **extra):
    payload = {'error': str(message)}
    payload.update(extra)
    return JsonResponse(payload, status=status, json_dumps_params={'ensure_ascii': False})


def _ok(payload):
    payload.setdefault('ok', True)
    return JsonResponse(payload, json_dumps_params={'ensure_ascii': False})


def _jalali_date(value):
    if not value:
        return ''
    if isinstance(value, jdatetime.date):
        return value.strftime('%Y/%m/%d')
    return jdatetime.date.fromgregorian(date=value).strftime('%Y/%m/%d')


def _rep_name(order):
    user = getattr(order, 'user', None)
    if user is None:
        return ''
    return user.get_full_name() or user.username


def _category_names(item_ids):
    """{item_id: real product category name} for display ordering (category first)."""
    from product.models import OrderItem
    ids = {int(item_id) for item_id in item_ids if item_id}
    if not ids:
        return {}
    rows = OrderItem.objects.filter(pk__in=ids).select_related('product__category')
    return {
        row.pk: (row.product.category.name if row.product_id and row.product.category_id else '')
        for row in rows
    }


def _attach_category(row, category_by_item):
    """Attach ``category_name`` so the UI can render «دسته محصول» before the name."""
    row['category_name'] = category_by_item.get(row.get('item_id'), '')
    return row


def _workspace_task_filter(prefix=''):
    """Tasks the workspace cares about: pending cut tasks with work left
    (raw or overridden quantity) plus anything an operator already touched.
    ``prefix='tasks__'`` re-anchors the same Q onto the Order relation."""
    p = f'{prefix}__' if prefix else ''
    has_work = (
        Q(**{f'{p}quantity__gt': F(f'{p}completed_quantity')})
        | Q(**{f'{p}spec_override__quantity__gt': F(f'{p}completed_quantity')})
    )
    pending = (
        Q(**{f'{p}station_name': 'cut'}) & Q(**{f'{p}part__isnull': False})
        & Q(**{f'{p}order__isnull': False}) & Q(**{f'{p}status': 'pending'}) & has_work
    )
    touched = (
        Q(**{f'{p}station_name': 'cut'}) & Q(**{f'{p}part__isnull': False})
        & Q(**{f'{p}order__isnull': False}) & Q(**{f'{p}spec_override__isnull': False})
    )
    return pending | touched


def _workspace_remaining_expr(prefix='tasks'):
    """بازبینی دور ۳: باقی‌ماندهٔ صف کارگاه همان دلالت specs را می‌شمارد.

    effective_spec (specs.py:187-198): override.quantity اگر ذکر شده جایگزین
    quantity است، excluded ⇒ صفر، و منفی با max(0,…) گیره می‌خورد. فرمول قبلی
    ‎Sum(quantity − completed)‎ روی همین فیلتر، اورراید را نادیده می‌گرفت و
    تسک‌های «خارج از برنامه» را هم با باقی‌ماندهٔ خام می‌شمرد — شمارهٔ کارت با
    جدول قطعات یکی در نمی‌ماند."""
    p = f'{prefix}__'
    base = Greatest(
        F(f'{p}quantity') - F(f'{p}completed_quantity'), Value(0),
        output_field=IntegerField(),
    )
    overridden = Greatest(
        F(f'{p}spec_override__quantity') - F(f'{p}completed_quantity'), Value(0),
        output_field=IntegerField(),
    )
    return Case(
        When(**{f'{p}spec_override__excluded': True}, then=Value(0)),
        When(**{f'{p}spec_override__isnull': False,
                f'{p}spec_override__quantity__isnull': True}, then=base),
        When(**{f'{p}spec_override__isnull': False}, then=overridden),
        default=base,
        output_field=IntegerField(),
    )


def _planned_order_ids(order_ids):
    """Orders with at least one task locked in a planned cut/CNC program."""
    from .models import CutPlanTask
    locked = set(CutPlanTask.objects.filter(
        plan__status='planned', task__order_id__in=order_ids,
    ).values_list('task__order_id', flat=True))
    from cnc_resizer.models import CncSheetPlan
    task_ids = set()
    # بازبینی دور ۳ (عملکرد): values_list به‌جای نمونهٔ مدل — فقط layout لازم است
    for _plan_id, layout in CncSheetPlan.objects.filter(status='planned').values_list('pk', 'layout'):
        for piece in (layout or {}).get('parts', []):
            if piece.get('task_id') is not None:
                task_ids.add(int(piece['task_id']))
    if task_ids:
        locked.update(ProductionTask.objects.filter(
            pk__in=task_ids, order_id__in=order_ids,
        ).values_list('order_id', flat=True))
    return locked


@require_GET
@cutting_operator
def workspace_orders(request):
    """Layer 1 — the order queue (searchable, filterable, paginated)."""
    task_filter = _workspace_task_filter()
    order_qs = Order.objects.filter(pk__in=ProductionTask.objects.filter(task_filter).values('order_id'))
    status_filter = request.GET.get('status', '')
    if status_filter in dict(Order.ORDER_STATUS):
        order_qs = order_qs.filter(status=status_filter)
    query = request.GET.get('q', '').strip()
    if query:
        search = Q(customer__name__icontains=query) | Q(number__icontains=query)
        if query.isdigit():
            search |= Q(pk=int(query))
        order_qs = order_qs.filter(search)
    orders = list(order_qs.select_related('customer', 'user').annotate(
        item_count=Count('tasks__order_item_id',
                         filter=Q(tasks__station_name='cut', tasks__part__isnull=False,
                                  tasks__order_item_id__isnull=False),
                         distinct=True),
        remaining_pieces=Sum(
            _workspace_remaining_expr(),
            filter=_workspace_task_filter('tasks'),
        ),
    ).order_by('priority', F('due_date').asc(nulls_last=True), '-id'))
    locked_ids = _planned_order_ids([order.pk for order in orders])
    try:
        page_size = min(max(int(request.GET.get('page_size', 25)), 1), 100)
    except (TypeError, ValueError):
        page_size = 25
    page = Paginator(orders, page_size).get_page(request.GET.get('page'))
    rows = [{
        'id': order.pk,
        'number': order.number or '',
        'rep_name': _rep_name(order),
        'customer_name': order.customer.name,
        'date_jalali': _jalali_date(order.created_at),
        'due_date_jalali': _jalali_date(order.due_date),
        'status': order.status,
        'status_label': order.get_status_display(),
        'item_count': order.item_count or 0,
        'remaining_pieces': order.remaining_pieces or 0,
        'planning_state': 'in_plan' if order.pk in locked_ids else 'idle',
    } for order in page.object_list]
    return _ok({
        'rows': rows,
        'count': len(orders),
        'page': page.number,
        'pages': Paginator(orders, page_size).num_pages,
        'statuses': list(Order.ORDER_STATUS),
    })


@require_GET
@cutting_operator
def workspace_order(request, order_id):
    """Layer 2 — the items of one order + the material chips for layer 3."""
    order = get_object_or_404(Order.objects.select_related('customer', 'user'), pk=order_id)
    tasks = list(workspace_task_queryset([order.pk]))
    specs = effective_specs(tasks)
    items = {item.pk: item for item in order.items.select_related('product__category').prefetch_related('ordercolor')}
    item_rows = {}
    for spec in specs:
        item_id = spec.item_id
        if item_id not in items:
            continue
        product = items[item_id].product
        row = item_rows.setdefault(item_id, {
            'id': item_id,
            'category_name': (product.category.name if product and product.category_id else ''),
            'product_name': items[item_id].product.name,
            'colors': items[item_id].color_summary,
            'size': items[item_id].size_display or '',
            'quantity': items[item_id].quantity,
            'material_names': [],
            'piece_count': 0,
        })
        row['piece_count'] += spec.quantity_remaining
        if spec.material_name not in row['material_names']:
            row['material_names'].append(spec.material_name)
    materials = []
    seen = set()
    for spec in specs:
        if spec.material_id in seen:
            continue
        seen.add(spec.material_id)
        materials.append({'id': spec.material_id, 'name': spec.material_name})
    materials.sort(key=lambda row: row['name'])
    return _ok({
        'order': {
            'id': order.pk, 'number': order.number or '',
            'customer_name': order.customer.name, 'rep_name': _rep_name(order),
            'date_jalali': _jalali_date(order.created_at),
            'status': order.status, 'status_label': order.get_status_display(),
        },
        'items': sorted(item_rows.values(), key=lambda row: row['id']),
        'materials': materials,
    })


def _spec_row(spec):
    return {
        'task_id': spec.task_id,
        'part_id': spec.part_id,
        'item_id': spec.item_id,
        'order_id': spec.order_id,
        'name': spec.name,
        'pname': spec.pname,
        'barcode': spec.barcode,
        'length': format(spec.length, 'f'),
        'width': format(spec.width, 'f'),
        'quantity_total': spec.quantity_total,
        'quantity_completed': spec.quantity_completed,
        'quantity_remaining': spec.quantity_remaining,
        'material_id': spec.material_id,
        'material_name': spec.material_name,
        'material_thickness': format(spec.material_thickness, 'f'),
        'turn': spec.turn,
        'grain': spec.grain,
        'edges': spec.edges,
        'excluded': spec.excluded,
        'excluded_reason': spec.excluded_reason,
        'is_cnc_first': spec.is_cnc_first,
        'overridden_fields': sorted(spec.overridden_fields),
        'reserved_plan': spec.reserved_plan,
        'version': spec.version or 0,
        'status_code': spec.status_code,
        'status_label': STATUS_LABELS.get(spec.status_code, spec.status_code),
        'routing_code': spec.routing_code,
        'editable': spec.status_code != 'locked_in_plan',
    }


SORT_KEYS = {
    'name': lambda spec: (spec.name, spec.task_id),
    'length': lambda spec: (spec.length, spec.task_id),
    'width': lambda spec: (spec.width, spec.task_id),
    'quantity': lambda spec: (spec.quantity_remaining, spec.task_id),
    'material': lambda spec: (spec.material_name, spec.task_id),
    'status': lambda spec: (spec.status_code, spec.task_id),
    'barcode': lambda spec: (spec.barcode, spec.task_id),
}


@require_GET
@cutting_operator
def workspace_parts(request, order_id):
    """Layer 3 — the editable parts table rows (+ per-material summary)."""
    order_exists = Order.objects.filter(pk=order_id).exists()
    if not order_exists:
        return _error('سفارش پیدا نشد.', status=404)
    tasks = list(workspace_task_queryset([order_id]))
    item_param = (request.GET.get('item') or 'all').strip()
    if item_param not in ('', 'all'):
        try:
            item_id = int(item_param)
        except ValueError:
            return _error('شناسه آیتم معتبر نیست.')
        tasks = [task for task in tasks if task.order_item_id == item_id]
    specs = effective_specs(tasks)
    material_param = (request.GET.get('material') or 'all').strip()
    if material_param not in ('', 'all'):
        try:
            material_id = int(material_param)
        except ValueError:
            return _error('شناسه متریال معتبر نیست.')
        specs = [spec for spec in specs if spec.material_id == material_id]
    query = request.GET.get('q', '').strip()
    if query:
        lowered = query.lower()
        specs = [
            spec for spec in specs
            if lowered in spec.name.lower() or lowered in spec.barcode.lower()
            or lowered in (spec.pname or '').lower()
        ]
    sort = request.GET.get('sort', '')
    if sort.startswith('-'):
        reverse = True
        key = SORT_KEYS.get(sort[1:])
    else:
        reverse = False
        key = SORT_KEYS.get(sort)
    if key:
        specs.sort(key=key, reverse=reverse)
    summary = {}
    for spec in specs:
        row = summary.setdefault(spec.material_id, {
            'material_id': spec.material_id, 'name': spec.material_name,
            'piece_need': 0, 'task_count': 0,
        })
        row['piece_need'] += spec.quantity_remaining
        row['task_count'] += 1
    try:
        page_size = min(max(int(request.GET.get('page_size', 50)), 1), 200)
    except (TypeError, ValueError):
        page_size = 50
    page = Paginator(specs, page_size).get_page(request.GET.get('page'))
    rows = [_spec_row(spec) for spec in page.object_list]
    category_by_item = _category_names(spec.item_id for spec in page.object_list)
    for row in rows:
        _attach_category(row, category_by_item)
    return _ok({
        'rows': rows,
        'count': len(specs),
        'page': page.number,
        'pages': Paginator(specs, page_size).num_pages,
        'summary': sorted(summary.values(), key=lambda row: row['name']),
    })


def _locked_task_response(task):
    spec = effective_spec(task)
    row = _spec_row(spec)
    _attach_category(row, _category_names([spec.item_id]))
    return row


def _version_guard(request, task, data):
    """409 when the client's row version is stale; returns (response|None, override)."""
    override = TaskSpecOverride.objects.select_for_update().filter(task=task).first()
    current_version = override.version if override is not None else 0
    raw_version = data.get('version')
    try:
        client_version = int(raw_version) if raw_version is not None else None
    except (TypeError, ValueError):
        client_version = None
    if client_version != current_version:
        return _error(
            'این ردیف توسط صفحهٔ دیگری تغییر کرده است؛ ردیف دوباره بارگذاری شد.',
            status=409,
            current_version=current_version,
            row=_locked_task_response(task),
        ), override
    return None, override


def _mutating_task(request, task_id, data):
    task = get_object_or_404(ProductionTask.objects.select_related(
        'part__material', 'order', 'spec_override__material'), pk=task_id)
    if task.part is None:
        raise ValueError('این تسک قطعه ندارد و از همین صفحه قابل ویرایش نیست.')
    response, _override = _version_guard(request, task, data)
    return task, response


@require_http_methods(['PATCH', 'POST'])
@cutting_operator
def workspace_task_spec(request, task_id):
    """Edit one row: {version, fields:{...}, reason?} (PATCH or POST)."""
    try:
        data = _json_body(request)
        with transaction.atomic():
            task, conflict = _mutating_task(request, task_id, data)
            if conflict is not None:
                return conflict
            fields = data.get('fields')
            if fields is None:
                fields = {key: value for key, value in data.items()
                          if key not in ('version', 'reason')}
            if not isinstance(fields, dict):
                raise ValueError('فیلدهای ویرایش معتبر نیستند.')
            reason = str(data.get('reason') or '')
            spec, events = apply_override(task, fields, request.user, reason)
    except SpecLockedError as exc:
        return _error(exc, status=409, row=_locked_task_response(
            get_object_or_404(ProductionTask.objects.select_related(
                'part__material', 'order', 'spec_override__material'), pk=task_id)))
    except ValueError as exc:
        return _error(exc)
    return _ok({
        'row': _spec_row(spec),
        'changed_fields': [event.field for event in events],
        'message': 'ذخیره شد.' if events else 'تغییری لازم نبود.',
    })


@require_POST
@cutting_operator
def workspace_task_exclude(request, task_id):
    """POST {version, reason} — «خارج از برنامه» با علت الزامی."""
    try:
        data = _json_body(request)
        with transaction.atomic():
            task, conflict = _mutating_task(request, task_id, data)
            if conflict is not None:
                return conflict
            spec, _events = set_excluded(
                task, excluded=True, reason=str(data.get('reason') or ''), user=request.user,
            )
    except SpecLockedError as exc:
        return _error(exc, status=409)
    except ValueError as exc:
        return _error(exc)
    return _ok({'row': _spec_row(spec), 'message': 'قطعه از برنامه خارج شد.'})


@require_POST
@cutting_operator
def workspace_task_include(request, task_id):
    """POST {version} — بازگرداندن قطعهٔ خارج‌شده به برنامه."""
    try:
        data = _json_body(request)
        with transaction.atomic():
            task, conflict = _mutating_task(request, task_id, data)
            if conflict is not None:
                return conflict
            spec, _events = set_excluded(task, excluded=False, user=request.user)
    except SpecLockedError as exc:
        return _error(exc, status=409)
    except ValueError as exc:
        return _error(exc)
    return _ok({'row': _spec_row(spec), 'message': 'قطعه دوباره وارد برنامه شد.'})


@require_POST
@cutting_operator
def workspace_task_reset(request, task_id):
    """POST {version} — پاک‌کردن همهٔ overrideها و بازگشت به مشخصات پایه."""
    try:
        data = _json_body(request)
        with transaction.atomic():
            task, conflict = _mutating_task(request, task_id, data)
            if conflict is not None:
                return conflict
            spec, _events = reset_override(task, request.user)
    except SpecLockedError as exc:
        return _error(exc, status=409)
    except ValueError as exc:
        return _error(exc)
    return _ok({'row': _spec_row(spec), 'message': 'ویرایش‌ها بازنشانی شد.'})
