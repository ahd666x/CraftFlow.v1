from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from functools import wraps
from math import ceil
import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_GET, require_POST
import jdatetime

from product.models import Material, Order, ProductionTask
from .models import (
    CuttingSettings, CutPlan, CutPlanSheet, CutPlanSheetTask, CutPlanTask, SheetSizeSpec,
    SheetStock, SheetStockEvent,
)
from .algorithms import (
    ENGINE_LABELS, ENGINE_ORDER, build_cut_sequence, compare_engines,
)
from .optimizer import expected_offcuts, make_autocut, optimize
from .permissions import can_inventory, can_manage, can_operate


MM = Decimal('0.1')
MAX_REMNANTS_PER_SHEET = 50


def cutting_operator(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not can_operate(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def inventory_operator(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not can_inventory(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def manager_only(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not can_manage(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def _dimension(value, field_name):
    try:
        value = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError):
        raise ValueError(f'{field_name} باید عددی باشد.')
    if not value.is_finite() or value <= 0:
        raise ValueError(f'{field_name} باید بزرگ‌تر از صفر باشد.')
    if value.quantize(MM) != value:
        raise ValueError(f'{field_name} حداکثر یک رقم اعشار می‌پذیرد؛ مقدار بدون گردکردن ثبت نشد.')
    return value.quantize(MM)


def _stock_policy(raw):
    return raw if raw in dict(CutPlan.STOCK_POLICIES) else 'offcuts_first'
def _json_safe(value):
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _reserved_counts(stock_ids=None):
    qs = CutPlanSheet.objects.filter(outcome='reserved', plan__status='planned')
    if stock_ids is not None:
        qs = qs.filter(source_stock_id__in=stock_ids)
    result = Counter()
    for stock_id in qs.values_list('source_stock_id', flat=True):
        result[stock_id] += 1
    # Sheets already assigned to a planned CNC (G-code) program are not free either.
    from cnc_resizer.models import CncSheetPlan
    cnc = CncSheetPlan.objects.filter(status='planned')
    if stock_ids is not None:
        cnc = cnc.filter(source_stock_id__in=stock_ids)
    for stock_id in cnc.values_list('source_stock_id', flat=True):
        result[stock_id] += 1
    return result


def _has_planned_cnc_reservation(stock_id):
    from cnc_resizer.models import CncSheetPlan
    return CncSheetPlan.objects.filter(source_stock_id=stock_id, status='planned').exists()


def _event(*, stock, event_type, user, quantity_delta=0, note='', plan=None, width=None, height=None, is_offcut=None):
    SheetStockEvent.objects.create(
        stock=stock, material=stock.material, plan=plan, event_type=event_type,
        quantity_delta=quantity_delta, width=width if width is not None else stock.width,
        height=height if height is not None else stock.height,
        is_offcut=stock.is_offcut if is_offcut is None else is_offcut,
        note=note[:255], created_by=user,
    )


@cutting_operator
def planner(request):
    materials = Material.objects.all().order_by('name')
    if request.method == 'POST' and request.POST.get('action') == 'plan':
        order_ids = request.POST.getlist('order_ids')
        cnc_sheet_mode = request.POST.get('planning_mode') == 'cnc_sheets'
        cnc_sheet_plans = []
        cnc_issues = []
        try:
            kerf = Decimal(request.POST.get('kerf', str(CuttingSettings.current().default_kerf)))
            if not kerf.is_finite() or kerf < 0 or kerf.quantize(MM) != kerf:
                raise ValueError('ضخامت تیغه باید صفر یا عدد مثبت با حداکثر یک رقم اعشار باشد.')
        except (InvalidOperation, ValueError):
            messages.error(request, 'مقدار تیغه معتبر نیست.'); return redirect('cutting:planner')
        policy = _stock_policy(request.POST.get('stock_policy'))
        engine_names = [name for name in request.POST.getlist('algorithms') if name in ENGINE_LABELS] or ['shelf']
        respect_grain = bool(request.POST.get('respect_grain'))
        try:
            trim = Decimal(request.POST.get('trim', '0') or '0')
            if not trim.is_finite() or trim < 0 or trim > 500 or trim.quantize(MM) != trim:
                raise ValueError('سربر باید بین صفر تا ۵۰۰ میلی‌متر با حداکثر یک رقم اعشار باشد.')
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, str(exc) if str(exc).startswith('سربر') else 'مقدار سربر معتبر نیست.')
            return redirect('cutting:planner')
        if not order_ids:
            messages.error(request, 'برای برنامه‌ریزی دست‌کم یک سفارش انتخاب کنید.'); return redirect('cutting:planner')
        if cnc_sheet_mode:
            try:
                from cnc_resizer.anomaly_report import find_unusual_parts
                unusual = find_unusual_parts(order_ids=order_ids)
                cnc_unusual = [row for row in unusual if (row['routing_code'] or '').strip().lower().split('.')[0:1] == ['cnc']]
                if cnc_unusual:
                    preview = '، '.join(
                        f"{row['part_name']} ({row['length']}×{row['width']})" for row in cnc_unusual[:5]
                    )
                    suffix = f' و {len(cnc_unusual) - 5} مورد دیگر' if len(cnc_unusual) > 5 else ''
                    messages.warning(request, f'قطعات غیرمعمول نیازمند بررسی: {preview}{suffix}')
                from cnc_resizer.sheet_nesting import create_cnc_sheet_plans
                cnc_sheet_plans, cnc_issues = create_cnc_sheet_plans(
                    order_ids, request.user, kerf=kerf,
                )
                if cnc_sheet_plans:
                    messages.success(request, f'{len(cnc_sheet_plans)} چیدمان ورق CNC ذخیره شد؛ فایل اجرایی تا تأیید کنترلر مقصد تولید نمی‌شود.')
                if cnc_issues:
                    messages.warning(request, f'{len(cnc_issues)} مورد نیازمند بررسی یا تأیید خروجی است؛ جزئیات در صفحهٔ برنامه‌های CNC ثبت شده است.')
            except (ValueError, OSError) as exc:
                messages.error(request, str(exc)); return redirect('cutting:planner')
            except Exception as exc:
                # The CNC resolver raises a domain error when an operator has
                # not yet approved the per-part CNC profile. Never expose a
                # traceback from the planning screen in that case.
                from cnc_resizer.services import CncResolutionError
                if isinstance(exc, CncResolutionError):
                    messages.error(
                        request,
                        f'{exc} — ابتدا از صفحهٔ اسکن قطعه، الگوی CNC را ثبت و تأیید کنید.',
                    )
                    return redirect('cutting:planner')
                raise
        try:
            with transaction.atomic():
                task_query = ProductionTask.objects.select_for_update().filter(
                    order_id__in=order_ids, station_name='cut', status='pending', part__isnull=False,
                    quantity__gt=F('completed_quantity'),
                )
                tasks = list(task_query.select_related('part__material', 'order', 'order__customer').order_by('pk'))
                from cnc_resizer.sheet_nesting import is_cnc_first
                tasks = [task for task in tasks if not is_cnc_first(task.part)]
                planned_cnc_counts = {}
                if not cnc_sheet_mode:
                    from cnc_resizer.sheet_nesting import _planned_piece_counts
                    planned_cnc_counts = _planned_piece_counts()
                remaining_by_task = {
                    task.pk: max(0, task.quantity - task.completed_quantity - planned_cnc_counts.get(task.pk, 0))
                    for task in tasks
                }
                tasks = [task for task in tasks if remaining_by_task[task.pk] > 0]
                if not tasks:
                    if cnc_sheet_plans or cnc_issues:
                        return redirect('cnc_resizer:sheet_plans')
                    raise ValueError('قطعه آماده برش برای سفارش‌های انتخاب‌شده پیدا نشد.')
                task_ids = [task.pk for task in tasks]
                if CutPlanTask.objects.filter(task_id__in=task_ids, plan__status='planned').exists():
                    raise ValueError('برخی از تسک‌ها هم‌اکنون در برنامه برش دیگری رزرو هستند.')
                stock_qs = SheetStock.objects.select_for_update().filter(
                    active=True, quantity__gt=0, material_id__in={t.part.material_id for t in tasks})
                stock_rows = list(stock_qs.select_related('material').order_by('priority', 'is_offcut', 'width', 'height', 'pk'))
                reserved = _reserved_counts([s.pk for s in stock_rows])
                stock_by_id = {s.pk: s for s in stock_rows}
                available = []
                for stock in stock_rows:
                    for _ in range(max(0, stock.quantity - reserved[stock.pk])):
                        available.append({
                            'stock_id': stock.pk, 'width': stock.width, 'height': stock.height,
                            'is_offcut': stock.is_offcut, 'priority': stock.priority,
                            'grain': stock.grain, 'label': f'{stock.material.name} {stock.width}×{stock.height}',
                        })
                grouped = defaultdict(list)
                for task in tasks:
                    part = task.part
                    if part.length <= 0 or part.width <= 0:
                        from cnc_resizer.models import CncPlanningIssue
                        CncPlanningIssue.objects.create(
                            task=task, order=task.order, part=part, mode='standard',
                            reason=f'ابعاد قطعه «{part.name or part.f3}» باید مثبت باشند.',
                            details={'stage': 'dimensions'}, created_by=request.user,
                        )
                        continue
                    remaining_quantity = remaining_by_task[task.pk]
                    for _ in range(remaining_quantity):
                        grouped[part.material_id].append({
                            # CUT convention: X = Part.length, Y = Part.width.
                            # The CNC G-code generator uses a separate Y=length convention.
                            'width': part.length, 'height': part.width,
                            'orig_width': part.length, 'orig_height': part.width,
                            'turn': part.turn and not (respect_grain and (part.grain or '').strip()),
                            'grain': part.grain, 'f2': part.f2 or part.name,
                            'f3': part.f3, 'routing': part.routing_code, 'order': str(task.order_id),
                            'customer': str(task.order.customer), 'label': f'{part.name} / سفارش {task.order_id}',
                            'uid': len(grouped[part.material_id]), 'task_id': task.pk,
                        })
                rows = []
                from cnc_resizer.models import CncPlanningIssue
                for material_id, parts in grouped.items():
                    material = next(task.part.material for task in tasks if task.part.material_id == material_id)
                    material_stock = [item for item in available if stock_by_id[item['stock_id']].material_id == material_id]
                    if not material_stock:
                        for task_id in {piece['task_id'] for piece in parts}:
                            task = next(task for task in tasks if task.id == task_id)
                            CncPlanningIssue.objects.create(
                                task=task, order=task.order, part=task.part, mode='standard',
                                reason=f'برای متریال «{material.name}» ورق آزاد موجود نیست.',
                                details={'stage': 'stock'}, created_by=request.user,
                            )
                        continue
                    remaining = list(parts)
                    layouts = []
                    row_report = []
                    while remaining:
                        try:
                            engine_results, chosen_name = compare_engines(
                                remaining, material_stock, kerf, policy=policy,
                                names=engine_names, trim=trim, respect_grain=respect_grain,
                            )
                            layouts = next(item['layouts'] for item in engine_results if item['algorithm'] == chosen_name)
                            row_report = [
                                {'algorithm': item['algorithm'], 'label': item['label'], 'ok': item['ok'],
                                 'sheet_count': item['sheet_count'],
                                 'utilization': format(item['utilization'].quantize(Decimal('0.01')), 'f') if item['utilization'] is not None else None,
                                 'error': item['error']}
                                for item in engine_results
                            ]
                            break
                        except (ValueError, TypeError) as exc:
                            candidate = next(
                                (piece for piece in remaining if piece.get('label', '') in str(exc)),
                                remaining[-1],
                            )
                            bad_task_id = candidate['task_id']
                            removed = [piece for piece in remaining if piece['task_id'] == bad_task_id]
                            remaining = [piece for piece in remaining if piece['task_id'] != bad_task_id]
                            task = next(task for task in tasks if task.id == bad_task_id)
                            CncPlanningIssue.objects.create(
                                task=task, order=task.order, part=task.part, mode='standard',
                                reason=str(exc), details={'stage': 'nesting', 'quantity': len(removed)},
                                created_by=request.user,
                            )
                    for layout in layouts:
                        layout['expected_offcuts'] = expected_offcuts(layout, kerf)
                    if layouts:
                        rows.append({
                            'type': material.name.replace(' ', '-'), 'thickness': str(material.thickness),
                            'sheets': layouts, 'shapes': remaining, 'material_id': material_id,
                            'algorithm': next((item['algorithm'] for item in row_report if item.get('ok')), 'shelf'),
                            'engine_report': row_report,
                        })
                planned_quantities = Counter()
                for row in rows:
                    for layout in row['sheets']:
                        for piece in layout['parts']:
                            if piece.get('task_id') is not None:
                                planned_quantities[int(piece['task_id'])] += 1
                orders = list(Order.objects.filter(pk__in=order_ids))
                plan_algorithm = 'shelf'
                if rows:
                    plan_algorithm = max(rows, key=lambda row: len(row['sheets'])).get('algorithm', 'shelf')
                plan_report = []
                for name in ENGINE_ORDER:
                    entries = [entry for row in rows for entry in row.get('engine_report', []) if entry['algorithm'] == name]
                    if not entries:
                        continue
                    ok_entries = [entry for entry in entries if entry['ok']]
                    plan_report.append({
                        'algorithm': name, 'label': ENGINE_LABELS[name], 'ok': bool(ok_entries),
                        'sheet_count': sum(entry['sheet_count'] or 0 for entry in ok_entries),
                        'utilization': (
                            format((sum((Decimal(entry['utilization']) for entry in ok_entries), Decimal('0')) / len(ok_entries)).quantize(Decimal('0.01')), 'f')
                            if ok_entries else None
                        ),
                        'error': next((entry['error'] for entry in entries if entry['error']), ''),
                    })
                plan = CutPlan.objects.create(
                    created_by=request.user, stock_policy=policy, algorithm=plan_algorithm,
                    algorithm_report=plan_report,
                    plan_data={'rows': [], 'kerf': str(kerf), 'policy': policy,
                               'algorithms': engine_names, 'respect_grain': respect_grain, 'trim': str(trim)},
                )
                plan.orders.set(orders)
                for task in tasks:
                    if not planned_quantities[task.pk]:
                        continue
                    CutPlanTask.objects.create(
                        plan=plan, task=task, quantity=planned_quantities[task.pk],
                        task_quantity=task.quantity, completed_before=task.completed_quantity,
                    )
                layout_index = defaultdict(int)
                for row in rows:
                    for layout in row['sheets']:
                        stock_data = layout['sheet']
                        source = stock_by_id[stock_data['stock_id']]
                        index = layout_index[row['material_id']]
                        layout_index[row['material_id']] += 1
                        sheet_record = CutPlanSheet.objects.create(
                            plan=plan, source_stock=source, material_id=row['material_id'],
                            layout_index=index, width=source.width, height=source.height,
                            planned_area=sum(
                                (Decimal(str(piece['width'])) * Decimal(str(piece['height']))
                                 for piece in layout['parts']), Decimal('0'),
                            ),
                            was_offcut=source.is_offcut,
                            cut_sequence=build_cut_sequence(layout, kerf, trim),
                        )
                        stock_data['plan_sheet_id'] = sheet_record.pk
                        pieces_by_task = Counter(
                            int(piece['task_id']) for piece in layout['parts']
                            if piece.get('task_id') is not None
                        )
                        for task_id, quantity in pieces_by_task.items():
                            CutPlanSheetTask.objects.create(
                                plan_sheet=sheet_record, task_id=task_id, quantity=quantity,
                            )
                        _event(stock=source, event_type='reserve', user=request.user, plan=plan, note=f'رزرو در برنامه #{plan.pk}')
                plan.plan_data = _json_safe({'rows': rows, 'kerf': kerf, 'policy': policy,
                                             'algorithms': engine_names, 'respect_grain': respect_grain, 'trim': trim})
                plan.save(update_fields=('plan_data',))
            return redirect('cutting:plan_detail', plan_id=plan.pk)
        except ValueError as exc:
            messages.error(request, str(exc)); return redirect('cutting:planner')
    reserved_order_ids = CutPlanTask.objects.filter(plan__status='planned').values('task__order_id')
    available_tasks = ProductionTask.objects.filter(
        station_name='cut', status='pending', part__isnull=False,
        quantity__gt=F('completed_quantity'), order__isnull=False,
    ).exclude(order_id__in=reserved_order_ids)
    order_qs = Order.objects.filter(pk__in=available_tasks.values('order_id')).select_related('customer')
    status_filter = request.GET.get('status', '')
    valid_statuses = dict(Order.ORDER_STATUS)
    if status_filter in valid_statuses:
        order_qs = order_qs.filter(status=status_filter)
    query = request.GET.get('q', '').strip()
    if query:
        search = Q(customer__name__icontains=query) | Q(number__icontains=query)
        if query.isdigit():
            search |= Q(pk=int(query))
        order_qs = order_qs.filter(search)
    orders = list(order_qs.annotate(
        cut_task_count=Count('tasks', filter=Q(tasks__pk__in=available_tasks), distinct=True),
        remaining_pieces=Sum(
            F('tasks__quantity') - F('tasks__completed_quantity'),
            filter=Q(tasks__pk__in=available_tasks),
        ),
    ).order_by('priority', F('due_date').asc(nulls_last=True), '-id'))
    material_names = defaultdict(list)
    if orders:
        material_rows = available_tasks.filter(order_id__in=[order.pk for order in orders]).values(
            'order_id', 'part__material__name',
        ).distinct().order_by('part__material__name')
        for material_row in material_rows:
            material_names[material_row['order_id']].append(material_row['part__material__name'])
        for order in orders:
            order.cut_material_names = material_names[order.pk]
            today_jalali = jdatetime.date.fromgregorian(date=timezone.localdate())
            order.cut_is_overdue = bool(order.due_date and order.due_date < today_jalali)
    active_plans = CutPlan.objects.filter(status='planned').annotate(
        reserved_sheet_count=Count('planned_sheets', distinct=True),
    ).prefetch_related('orders').order_by('-created_at')[:10]
    setting = CuttingSettings.current()
    return render(request, 'cutting/planner.html', {
        'materials': materials, 'orders': orders, 'setting': setting,
        'order_statuses': Order.ORDER_STATUS, 'status_filter': status_filter, 'query': query,
        'active_plans': active_plans, 'engine_choices': CutPlan.ALGORITHMS,
    })


@cutting_operator
@require_GET
def planner_preview(request):
    """Read-only estimate for selected orders; it never reserves stock."""
    raw_order_ids = request.GET.getlist('order_ids')
    if len(raw_order_ids) > 100:
        return JsonResponse({'error': 'حداکثر 100 سفارش را هم‌زمان انتخاب کنید.'}, status=400)
    try:
        order_ids = list(dict.fromkeys(int(value) for value in raw_order_ids))
        kerf = Decimal(request.GET.get('kerf', str(CuttingSettings.current().default_kerf)))
        if any(value <= 0 for value in order_ids) or not kerf.is_finite() or kerf < 0 or kerf.quantize(MM) != kerf:
            raise ValueError
    except (TypeError, ValueError, InvalidOperation):
        return JsonResponse({'error': 'شناسه سفارش یا ضخامت تیغه معتبر نیست.'}, status=400)
    if not order_ids:
        return JsonResponse({'selected_order_count': 0, 'piece_count': 0, 'materials': []})

    reserved_order_ids = CutPlanTask.objects.filter(plan__status='planned').values('task__order_id')
    tasks = list(ProductionTask.objects.filter(
        order_id__in=order_ids, station_name='cut', status='pending', part__isnull=False,
        quantity__gt=F('completed_quantity'),
    ).exclude(order_id__in=reserved_order_ids).select_related(
        'part__material', 'order', 'order__customer',
    ).order_by('pk'))
    cnc_mode = request.GET.get('planning_mode') == 'cnc_sheets'
    from cnc_resizer.sheet_nesting import is_cnc_first
    if cnc_mode:
        tasks = [task for task in tasks if is_cnc_first(task.part)]
        from cnc_resizer.sheet_nesting import _planned_piece_counts
        already_planned = _planned_piece_counts()
        piece_quantities = {
            task.pk: max(0, task.quantity - task.completed_quantity - already_planned.get(task.pk, 0))
            for task in tasks
        }
        tasks = [task for task in tasks if piece_quantities[task.pk] > 0]
    else:
        tasks = [task for task in tasks if not is_cnc_first(task.part)]
        from cnc_resizer.sheet_nesting import _planned_piece_counts
        already_planned = _planned_piece_counts()
        piece_quantities = {
            task.pk: max(0, task.quantity - task.completed_quantity - already_planned.get(task.pk, 0))
            for task in tasks
        }
        tasks = [task for task in tasks if piece_quantities[task.pk] > 0]
    total_pieces = sum(piece_quantities[task.pk] for task in tasks)
    invalid_part = next((task.part for task in tasks if task.part.length <= 0 or task.part.width <= 0), None)
    if invalid_part:
        return JsonResponse({'error': f'ابعاد قطعه «{invalid_part.name}» باید مثبت باشند.'}, status=400)
    if len(tasks) > 2000 or total_pieces > 3000:
        return JsonResponse({'error': 'این انتخاب برای پیش‌نمایش خیلی بزرگ است؛ سفارش‌ها را به چند برنامه تقسیم کنید.'}, status=400)

    policy = _stock_policy(request.GET.get('stock_policy'))
    setting = CuttingSettings.current()
    stocks = list(SheetStock.objects.filter(
        active=True, quantity__gt=0, material_id__in={task.part.material_id for task in tasks},
    ).select_related('material').order_by('priority', 'is_offcut', 'width', 'height', 'pk'))
    reserved = _reserved_counts([stock.pk for stock in stocks])
    stock_by_material = defaultdict(list)
    for stock in stocks:
        if cnc_mode and stock.is_offcut:
            continue
        for _ in range(max(0, stock.quantity - reserved[stock.pk])):
            stock_by_material[stock.material_id].append({
                'stock_id': stock.pk, 'width': stock.width, 'height': stock.height,
                'is_offcut': stock.is_offcut, 'priority': stock.priority,
                'grain': stock.grain, 'label': f'{stock.material.name} {stock.width}×{stock.height}',
            })

    grouped = defaultdict(list)
    for task in tasks:
        part = task.part
        for _ in range(piece_quantities[task.pk]):
            part_width, part_height = (part.width, part.length) if cnc_mode else (part.length, part.width)
            grouped[part.material_id].append({
                # CNC convention: X = Part.width, Y = Part.length; CUT uses the reverse.
                'width': part_width, 'height': part_height, 'orig_width': part_width,
                'orig_height': part_height, 'turn': part.turn, 'grain': part.grain,
                'label': f'{part.name} / سفارش {task.order_id}', 'uid': len(grouped[part.material_id]),
                'task_id': task.pk,
            })

    material_results = []
    material_by_id = {item.pk: item for item in Material.objects.filter(pk__in=grouped)}
    for material_id, parts in grouped.items():
        material = material_by_id[material_id]
        stock = stock_by_material[material_id]
        required_area = sum((Decimal(str(p['width'])) * Decimal(str(p['height'])) for p in parts), Decimal('0'))
        available_area = sum((Decimal(str(s['width'])) * Decimal(str(s['height'])) for s in stock), Decimal('0'))
        size = SheetSizeSpec.objects.filter(material_id=material_id, active=True, is_default=True).first()
        if not size:
            size = SheetSizeSpec.objects.filter(material_id=material_id, active=True).order_by('pk').first()
        reference_area = Decimal(str(size.width)) * Decimal(str(size.height)) if size else max(
            (Decimal(str(s['width'])) * Decimal(str(s['height'])) for s in stock), default=Decimal('0'),
        )
        rough_estimate = ceil(required_area / reference_area) if reference_area else None
        fits, exact_estimate = False, None
        try:
            exact_estimate = len(optimize(parts, stock, kerf, policy=policy))
            fits = True
        except ValueError:
            pass
        estimate = exact_estimate if fits else rough_estimate
        additional = 0 if fits else (estimate - len(stock) if estimate is not None and estimate > len(stock) else None)
        material_results.append({
            'material_id': material_id, 'name': material.name, 'thickness': format(material.thickness, 'f'),
            'piece_count': len(parts), 'required_area_m2': format(required_area / Decimal('1000000'), '.3f'),
            'available_sheets': len(stock), 'available_area_m2': format(available_area / Decimal('1000000'), '.3f'),
            'available_full_sheets': sum(not sheet['is_offcut'] for sheet in stock),
            'available_offcut_sheets': sum(bool(sheet['is_offcut']) for sheet in stock),
            'estimated_sheets': estimate, 'estimate_is_exact': fits, 'estimated_additional_sheets': additional,
            'fits': fits,
        })
    return JsonResponse({
        'selected_order_count': len({task.order_id for task in tasks}),
        'piece_count': total_pieces, 'strategy': dict(CutPlan.STOCK_POLICIES)[policy], 'materials': material_results,
    })


@cutting_operator
def plan_list(request):
    plans = CutPlan.objects.prefetch_related('orders').annotate(sheet_count=Count('planned_sheets', distinct=True))
    status_filter = request.GET.get('status', '')
    if status_filter in dict(CutPlan.STATUS):
        plans = plans.filter(status=status_filter)
    query = request.GET.get('q', '').strip()
    if query:
        search = Q(orders__customer__name__icontains=query) | Q(orders__number__icontains=query)
        if query.isdigit():
            search |= Q(pk=int(query)) | Q(orders__pk=int(query))
        plans = plans.filter(search).distinct()
    page = Paginator(plans.order_by('-created_at', '-pk'), 40).get_page(request.GET.get('page'))
    for plan in page.object_list:
        rows = plan.plan_data.get('rows', [])
        plan.part_count = sum(len(row.get('shapes', [])) for row in rows)
        plan.material_names = ', '.join(row.get('type', '') for row in rows)
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'cutting/plan_list.html', {
        'page': page, 'status_filter': status_filter, 'statuses': CutPlan.STATUS,
        'query': query, 'pagination_query': params.urlencode(),
    })


@cutting_operator
def plan_detail(request, plan_id):
    def mm_text(value):
        """AutoSAW-style dimension text: 400 instead of 400.0."""
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError):
            return str(value)
        if number == number.to_integral_value():
            return str(int(number))
        return format(number.normalize(), 'f')

    plan = get_object_or_404(CutPlan.objects.prefetch_related(
        'orders', 'planned_sheets__material', 'planned_sheets__source_stock', 'planned_sheets__task_counts',
    ), pk=plan_id)
    summaries = []
    planned_by_id = {s.pk: s for s in plan.planned_sheets.all()}
    # AutoSAW semantics: green = consumed, periwinkle = waiting, red = scrapped.
    color_legend = [
        {'color': '#16a34a', 'label': 'قطعهٔ مصرف‌شده'},
        {'color': '#6666c8', 'label': 'در انتظار برش'},
        {'color': '#e11d48', 'label': 'ورق اسقاطی'},
    ]
    total_sheet_area = Decimal('0')
    total_piece_area = Decimal('0')
    plan_trim = Decimal(str(plan.plan_data.get('trim') or '0'))
    for row in plan.plan_data.get('rows', []):
        row_piece_area = Decimal('0')
        need_by_task = Counter(shape.get('task_id') for shape in row.get('shapes', []))
        cut_by_task = Counter()
        verlist, seen_tasks = [], set()
        for layout in row['sheets']:
            layout['sheet_record'] = planned_by_id.get(layout['sheet'].get('plan_sheet_id'))
            longest = max(Decimal(str(layout['sheet']['width'])), Decimal(str(layout['sheet']['height'])))
            layout['label_font'] = max(14, int(longest / 40))
            layout['dimension_font'] = max(10, int(layout['label_font'] * 0.3))
            layout['expected_offcuts_json'] = mark_safe(json.dumps(layout.get('expected_offcuts', []), default=str))
            layout['trim_mm'] = plan_trim if plan_trim > 0 else None
            if layout['trim_mm'] is not None:
                layout['trim_label_font'] = int(min(plan_trim / Decimal('2'), Decimal('70')))
            sheet_area = Decimal(str(layout['sheet']['width'])) * Decimal(str(layout['sheet']['height']))
            piece_area = sum((Decimal(str(part['width'])) * Decimal(str(part['height'])) for part in layout['parts']), Decimal('0'))
            layout['sheet_area_mm2'] = sheet_area
            layout['piece_area_mm2'] = piece_area
            layout['utilization_percent'] = (piece_area / sheet_area * 100) if sheet_area else Decimal('0')
            record = layout['sheet_record']
            layout['planned_piece_count'] = sum(snapshot.quantity for snapshot in record.task_counts.all()) if record else len(layout['parts'])
            sheet_state = record.outcome if record else 'planned'
            if record and record.outcome == 'used':
                cut_by_task.update(part.get('task_id') for part in layout['parts'] if part.get('task_id') is not None)
            # Machine frame: the saw cuts the trim head first, so display
            # coordinates start below the strip while the stored/.CUT XML
            # coordinates (usable-height frame, y=0 based) stay untouched.
            if plan_trim > 0:
                for part in layout['parts']:
                    part['y'] = Decimal(str(part['y'])) + plan_trim
                for rem in layout.get('expected_offcuts', []):
                    if rem.get('y') is not None:
                        rem['y'] = Decimal(str(rem['y'])) + plan_trim
            for number, part in enumerate(layout['parts'], 1):
                part['no'] = number
                # Label geometry mirrors AutoSAW Process view: two white lines
                # (piece number + W × H) centred in the piece, rotated 90° for
                # portrait strips. Regression note: the old horizontal
                # label_x/dimension_y pair overlapped on narrow pieces.
                width = Decimal(str(part['width']))
                height = Decimal(str(part['height']))
                center_x = Decimal(str(part['x'])) + width / 2
                center_y = Decimal(str(part['y'])) + height / 2
                vertical = height > width
                across, along = (width, height) if vertical else (height, width)
                dims_text = f'{mm_text(part["width"])} × {mm_text(part["height"])}'
                fit = min(
                    across / Decimal('2.6'),
                    along / (Decimal('0.64') * max(len(dims_text), 6)),
                    Decimal('130'),
                )
                if min(width, height) < 150 or fit < 34:
                    show_dims = False
                    font = max(Decimal('24'), min(across / Decimal('2.2'), along / Decimal('3'), Decimal('150')))
                else:
                    show_dims = True
                    font = fit
                part['cx'] = center_x
                part['cy'] = center_y
                part['vtext'] = vertical
                part['font'] = int(font)
                part['dim_font'] = int(font * Decimal('0.6'))
                part['dy1'] = int(-font * Decimal('0.55'))
                part['dy2'] = int(font * Decimal('0.6') * Decimal('0.78'))
                part['show_dims'] = show_dims
                part['dims'] = dims_text
                part['used'] = sheet_state == 'used'
                part['scrapped'] = sheet_state == 'scrapped'
                part['color'] = '#16a34a' if part['used'] else ('#e11d48' if part['scrapped'] else '#6666c8')
            no_by_uid = {part.get('uid'): part['no'] for part in layout['parts']}
            raw_sequence = list(record.cut_sequence or []) if record else []
            mapped_sequence = []
            for cut in raw_sequence:
                pieces = [no_by_uid.get(uid) for uid in cut.get('pieces', [])]
                pieces = [piece for piece in pieces if piece]
                if pieces or cut.get('t') == 'trim':
                    at = cut.get('at')
                    if plan_trim > 0 and cut.get('t') != 'trim' and cut.get('axis', 'y') == 'y' and at is not None:
                        at = Decimal(str(at)) + plan_trim
                    mapped_sequence.append({
                        't': cut.get('t', 'rip'), 'axis': cut.get('axis', 'y'),
                        'at': at, 'pieces': pieces,
                    })
            layout['cut_sim_seq'] = mark_safe(json.dumps(mapped_sequence, default=str)) if mapped_sequence else ''
            row_piece_area += piece_area
            total_sheet_area += sheet_area
            total_piece_area += piece_area
        for shape in row.get('shapes', []):
            task_id = shape.get('task_id')
            if task_id in seen_tasks:
                continue
            seen_tasks.add(task_id)
            verlist.append({
                'code': shape.get('f3') or (shape.get('label') or '').split(' / ')[0],
                'name': shape.get('f2') or shape.get('label') or '',
                'width': mm_text(shape.get('width')), 'height': mm_text(shape.get('height')),
                'need': need_by_task.get(task_id, 0), 'cut': cut_by_task.get(task_id, 0),
            })
        summaries.append({
            'material': row['type'], 'thickness': row['thickness'],
            'sheet_count': len(row['sheets']), 'part_count': len(row['shapes']),
            'piece_area_mm2': row_piece_area, 'sheets': row['sheets'],
            'verlist': verlist,
        })
    plan_metrics = {
        'sheet_count': sum(summary['sheet_count'] for summary in summaries),
        'piece_count': sum(summary['part_count'] for summary in summaries),
        'sheet_area_m2': total_sheet_area / Decimal('1000000'),
        'piece_area_m2': total_piece_area / Decimal('1000000'),
        'waste_area_m2': max(Decimal('0'), total_sheet_area - total_piece_area) / Decimal('1000000'),
        'utilization_percent': (total_piece_area / total_sheet_area * 100) if total_sheet_area else Decimal('0'),
    }
    outcome_counts = Counter(
        sheet.outcome for sheet in plan.planned_sheets.all()
    )
    sheet_progress = {
        'total': sum(outcome_counts.values()),
        'reserved': outcome_counts.get('reserved', 0),
        'used': outcome_counts.get('used', 0),
        'untouched': outcome_counts.get('untouched', 0),
        'scrapped': outcome_counts.get('scrapped', 0),
        'completed': sum(outcome_counts.get(key, 0) for key in ('used', 'untouched', 'scrapped')),
    }
    sheet_progress['percent'] = (
        sheet_progress['completed'] * 100 / sheet_progress['total']
        if sheet_progress['total'] else 0
    )
    unfinished_tasks = [row for row in plan.planned_tasks.select_related('task', 'task__part') if row.task.status != 'done']
    return render(request, 'cutting/plan_detail.html', {
        'plan': plan, 'summaries': summaries, 'unfinished_tasks': unfinished_tasks,
        'plan_metrics': plan_metrics, 'sheet_progress': sheet_progress,
        'color_legend': color_legend,
    })


@cutting_operator
def download_cut(request, plan_id):
    plan = get_object_or_404(CutPlan, pk=plan_id)
    payload = make_autocut(plan.pk, plan.plan_data.get('rows', []), Decimal(plan.plan_data.get('kerf', '4')))
    response = HttpResponse(payload, content_type='application/xml; charset=utf-16')
    response['Content-Disposition'] = f'attachment; filename="craftflow_cut_plan_{plan.pk}.CUT"'
    return response


def _parse_sheet_result(request, sheet):
    outcome = request.POST.get(f'outcome_{sheet.pk}', 'used')
    if outcome not in ('used', 'untouched', 'scrapped'):
        raise ValueError('نتیجه یکی از ورق‌ها معتبر نیست.')
    if outcome == 'scrapped' and not request.POST.get(f'note_{sheet.pk}', '').strip():
        raise ValueError('برای اسقاط ورق، علت را در یادداشت همان ورق ثبت کنید.')
    if outcome != 'used':
        return outcome, []
    remnants = []
    pattern = re.compile(rf'^rem_{sheet.pk}_(\d+)_(width|height)$')
    posted_rows = {}
    for key in request.POST.keys():
        match = pattern.match(key)
        if not match:
            continue
        index, dimension = int(match.group(1)), match.group(2)
        if index < 1 or index > MAX_REMNANTS_PER_SHEET:
            raise ValueError(f'برای هر ورق حداکثر {MAX_REMNANTS_PER_SHEET} پرت قابل ثبت است.')
        posted_rows.setdefault(index, {})[dimension] = request.POST.get(key, '').strip()
    for index, row in sorted(posted_rows.items()):
        raw_w = row.get('width', '')
        raw_h = row.get('height', '')
        if not raw_w and not raw_h:
            continue
        if not raw_w or not raw_h:
            raise ValueError('برای هر پرت واقعی باید هم عرض و هم طول وارد شود.')
        width = _dimension(raw_w, 'عرض پرت')
        height = _dimension(raw_h, 'طول پرت')
        if width > sheet.width or height > sheet.height:
            raise ValueError('ابعاد پرت نمی‌تواند از ابعاد ورق اولیه بزرگ‌تر باشد.')
        remnants.append({'width': format(width, 'f'), 'height': format(height, 'f')})
    remnant_area = sum((Decimal(item['width']) * Decimal(item['height']) for item in remnants), Decimal('0'))
    if remnant_area > sheet.width * sheet.height:
        raise ValueError('مجموع مساحت پرت‌های واردشده از مساحت ورق مبدأ بیشتر است.')
    if remnants and (sheet.planned_area <= 0 or remnant_area > sheet.width * sheet.height - sheet.planned_area):
        raise ValueError('مجموع مساحت پرت‌ها از فضای باقی‌مانده پس از قطعات برنامه بیشتر است؛ نتیجه ثبت نشد.')
    return outcome, remnants


@cutting_operator
@require_POST
def mark_cut(request, plan_id):
    try:
        with transaction.atomic():
            plan = get_object_or_404(CutPlan.objects.select_for_update(), pk=plan_id)
            if plan.status in ('cut', 'partial'):
                messages.info(request, 'این برنامه قبلاً ثبت نهایی شده است؛ موجودی دوباره کسر نشد.')
                return redirect('cutting:plan_detail', plan_id=plan.pk)
            if plan.status != 'planned':
                raise ValueError('برنامه لغو شده و قابل ثبت نهایی نیست.')
            sheets = list(CutPlanSheet.objects.select_for_update().filter(plan=plan).select_related('source_stock', 'material').prefetch_related('task_counts').order_by('pk'))
            task_rows = list(CutPlanTask.objects.select_for_update().filter(plan=plan).select_related('task'))
            if not sheets or not task_rows:
                raise ValueError('این برنامه قدیمی یا ناقص است؛ برای ایمنی موجودی، برنامه را دوباره ایجاد کنید.')
            if any(row.task.status != 'pending' or row.task.quantity != row.task_quantity or row.task.completed_quantity != row.completed_before for row in task_rows):
                raise ValueError('تسک‌های برنامه از زمان ایجاد تغییر کرده‌اند؛ برنامه را دوباره بسازید.')
            planned_piece_counts = Counter()
            for sheet in sheets:
                for snapshot in sheet.task_counts.all():
                    planned_piece_counts[snapshot.task_id] += snapshot.quantity
            expected_piece_counts = {row.task_id: row.quantity for row in task_rows}
            if planned_piece_counts != Counter(expected_piece_counts):
                raise ValueError('snapshot قطعه‌های برنامه ناقص یا ناسازگار است؛ این برنامه را لغو و دوباره ایجاد کنید.')
            results = {sheet.pk: _parse_sheet_result(request, sheet) for sheet in sheets}
            stock_ids = {sheet.source_stock_id for sheet in sheets}
            locked_stock = {row.pk: row for row in SheetStock.objects.select_for_update().filter(pk__in=stock_ids)}
            usage = Counter(sheet.source_stock_id for sheet in sheets if results[sheet.pk][0] in ('used', 'scrapped'))
            reserved_elsewhere = _reserved_counts(stock_ids)
            reservations_in_this_plan = Counter(sheet.source_stock_id for sheet in sheets)
            for stock_id, count in usage.items():
                source = locked_stock.get(stock_id)
                if source is None or not source.active or source.quantity < count:
                    raise ValueError('موجودی رزروشده دیگر در دسترس نیست؛ ثبت عملیات لغو شد و موجودی تغییر نکرد.')
                future_reserved = reserved_elsewhere[stock_id] - reservations_in_this_plan[stock_id]
                if source.quantity - count < future_reserved:
                    raise ValueError('کسر این ورق‌ها باعث کمبود رزرو برنامه دیگری می‌شود؛ عملیات لغو شد.')
            # Apply the operator-confirmed outcomes. Untouched sheets stay in their original stock batch.
            for sheet in sheets:
                outcome, remnants = results[sheet.pk]
                source = locked_stock[sheet.source_stock_id]
                sheet.outcome = outcome
                sheet.actual_offcuts = remnants
                sheet.result_note = request.POST.get(f'note_{sheet.pk}', '').strip()[:255]
                sheet.save(update_fields=('outcome', 'actual_offcuts', 'result_note'))
                if outcome == 'untouched':
                    _event(stock=source, event_type='untouched', user=request.user, plan=plan, note=f'برنامه #{plan.pk}: ورق مصرف نشد')
                    continue
                source.quantity -= 1
                if source.quantity == 0:
                    source.active = False
                    source.inactive_reason = f'مصرف کامل در برنامه برش #{plan.pk}'
                    source.save(update_fields=('quantity', 'active', 'inactive_reason', 'updated_at'))
                else:
                    source.save(update_fields=('quantity', 'updated_at'))
                event_type = 'scrap' if outcome == 'scrapped' else 'consume'
                _event(stock=source, event_type=event_type, user=request.user, quantity_delta=-1, plan=plan, note=sheet.result_note or f'مصرف در برنامه #{plan.pk}')
                if outcome == 'scrapped':
                    continue
                for remnant in remnants:
                    width = Decimal(remnant['width']); height = Decimal(remnant['height'])
                    offcut = SheetStock.objects.create(
                        material=sheet.material, width=width, height=height, quantity=1, is_offcut=True,
                        active=True, priority=source.priority, grain=source.grain,
                        note=f'پرت واقعی برنامه #{plan.pk}', origin=source, created_from_plan=plan, created_by=request.user,
                    )
                    _event(stock=offcut, event_type='offcut', user=request.user, quantity_delta=1, plan=plan, note=f'پرت تأییدشده از ورق #{source.pk}')
            # Count only pieces on sheets the operator actually cut; incomplete tasks remain
            # pending with their completed_quantity, so a later plan contains only the remainder.
            cut_piece_counts = Counter()
            for sheet in sheets:
                if results[sheet.pk][0] == 'used':
                    for snapshot in sheet.task_counts.all():
                        cut_piece_counts[snapshot.task_id] += snapshot.quantity
            all_tasks_complete = True
            # Only exact tasks in this plan are updated, in the same transaction.
            for row in task_rows:
                row.task.completed_quantity = min(row.task_quantity, row.completed_before + cut_piece_counts[row.task_id])
                row.task.status = 'done' if row.task.completed_quantity >= row.task_quantity else 'pending'
                if cut_piece_counts[row.task_id]:
                    row.task.scanned_by = request.user
                if row.task.status != 'done':
                    all_tasks_complete = False
                row.task.save()
            plan.status = 'cut' if all_tasks_complete else 'partial'
            plan.cut_at = timezone.now(); plan.save(update_fields=('status', 'cut_at'))
        if all_tasks_complete:
            messages.success(request, 'نتیجه واقعی برش ذخیره شد؛ فقط ورق‌های مصرف‌شده کسر و تسک‌های کامل‌شده ثبت شدند.')
        else:
            messages.warning(request, 'نتیجه این برنامه جزئی بود؛ تسک‌های ناتمام با تعداد باقی‌مانده در فهرست برنامه‌ریزی می‌مانند.')
        return redirect('cutting:plan_detail', plan_id=plan.pk)
    except ValueError as exc:
        messages.error(request, str(exc)); return redirect('cutting:plan_detail', plan_id=plan_id)


@cutting_operator
@require_POST
def cancel_plan(request, plan_id):
    with transaction.atomic():
        plan = get_object_or_404(CutPlan.objects.select_for_update(), pk=plan_id)
        if plan.status != 'planned':
            messages.info(request, 'فقط برنامه‌ی در انتظار برش قابل لغو است.')
            return redirect('cutting:plan_detail', plan_id=plan.pk)
        sheets = list(CutPlanSheet.objects.select_for_update().filter(plan=plan).select_related('source_stock'))
        for sheet in sheets:
            _event(stock=sheet.source_stock, event_type='release', user=request.user, plan=plan, note=f'لغو برنامه #{plan.pk}')
            sheet.outcome = 'released'; sheet.save(update_fields=('outcome',))
        plan.status = 'cancelled'; plan.cancelled_at = timezone.now(); plan.save(update_fields=('status', 'cancelled_at'))
    messages.success(request, 'برنامه لغو و رزروها آزاد شدند؛ موجودی واقعی تغییری نکرد.')
    return redirect('cutting:plan_detail', plan_id=plan.pk)


def _inventory_queryset(request, kind=None):
    inactive = request.GET.get('inactive') == '1'
    qs = SheetStock.objects.filter(active=False) if inactive else SheetStock.objects.filter(active=True, quantity__gt=0)
    qs = qs.select_related('material').annotate(
        reserved_quantity=Count(
            'plan_reservations',
            filter=Q(plan_reservations__outcome='reserved', plan_reservations__plan__status='planned'),
            distinct=True,
        ),
    ).annotate(available_quantity=F('quantity') - F('reserved_quantity'))
    if kind == 'full': qs = qs.filter(is_offcut=False)
    elif kind == 'offcuts': qs = qs.filter(is_offcut=True)
    query = request.GET.get('q', '').strip()
    if query:
        dimensions = query.lower().replace('×', 'x').split('x')
        if len(dimensions) == 2:
            try:
                first, second = Decimal(dimensions[0].strip()), Decimal(dimensions[1].strip())
                qs = qs.filter((Q(width=first, height=second) | Q(width=second, height=first)) | Q(material__name__icontains=query) | Q(note__icontains=query))
            except InvalidOperation:
                qs = qs.filter(Q(material__name__icontains=query) | Q(note__icontains=query))
        else:
            try:
                dim = Decimal(query)
                qs = qs.filter(Q(material__name__icontains=query) | Q(width=dim) | Q(height=dim) | Q(note__icontains=query))
            except InvalidOperation:
                qs = qs.filter(Q(material__name__icontains=query) | Q(note__icontains=query))
    if request.GET.get('material'):
        qs = qs.filter(material_id=request.GET['material'])
    if request.GET.get('thickness'):
        qs = qs.filter(material__thickness=request.GET['thickness'])
    sort_map = {
        'material': ('material__name', 'width', 'height', 'pk'),
        'width': ('width', 'height', 'pk'), '-width': ('-width', 'height', 'pk'),
        'height': ('height', 'width', 'pk'), '-height': ('-height', 'width', 'pk'),
        'quantity': ('quantity', 'pk'), '-quantity': ('-quantity', 'pk'),
        'priority': ('priority', 'pk'), '-priority': ('-priority', 'pk'),
        'date': ('-created_at', '-pk'),
    }
    sort = request.GET.get('sort', 'material')
    qs = qs.order_by(*sort_map.get(sort, sort_map['material']))
    return qs


@inventory_operator
def inventory(request, kind=None):
    active = SheetStock.objects.filter(active=True, quantity__gt=0)
    full_count = active.filter(is_offcut=False).aggregate(total=Sum('quantity'))['total'] or 0
    offcut_count = active.filter(is_offcut=True).aggregate(total=Sum('quantity'))['total'] or 0
    total_count = full_count + offcut_count
    summaries = list(active.values('material_id', 'material__name', 'material__thickness', 'is_offcut').annotate(units=Sum('quantity')).order_by('material__name', 'is_offcut'))
    reserved_by_stock = _reserved_counts()
    reserved_by_group = defaultdict(int)
    stock_dimensions = {
        row['pk']: row for row in SheetStock.objects.filter(pk__in=reserved_by_stock).values('pk', 'material_id', 'is_offcut')
    }
    for stock_id, reserved_units in reserved_by_stock.items():
        stock_row = stock_dimensions.get(stock_id)
        if stock_row:
            reserved_by_group[(stock_row['material_id'], stock_row['is_offcut'])] += reserved_units
    for summary in summaries:
        summary['reserved'] = min(summary['units'], reserved_by_group.get((summary['material_id'], summary['is_offcut']), 0))
        summary['available'] = summary['units'] - summary['reserved']
    reserved_count = sum(row['reserved'] for row in summaries)
    available_count = total_count - reserved_count
    event_totals = SheetStockEvent.objects.filter(
        event_type__in=('consume', 'scrap'), quantity_delta__lt=0,
    ).values('event_type').annotate(total=Sum('quantity_delta'))
    event_totals = {row['event_type']: abs(row['total'] or 0) for row in event_totals}
    rows = _inventory_queryset(request, kind)
    page = Paginator(rows, 40).get_page(request.GET.get('page'))
    reserved_by_stock = _reserved_counts([stock.pk for stock in page.object_list])
    for stock in page.object_list:
        stock.reserved_quantity = reserved_by_stock.get(stock.pk, 0)
        stock.available_quantity = max(0, stock.quantity - stock.reserved_quantity)
        stock.reserve_percent = min(100, round(100 * stock.reserved_quantity / stock.quantity)) if stock.quantity else 0
        longest = max(float(stock.width), float(stock.height), 1)
        stock.visual_width_px = max(12, round(58 * float(stock.width) / longest))
        stock.visual_height_px = max(10, round(32 * float(stock.height) / longest))
    pagination_params = request.GET.copy()
    pagination_params.pop('page', None)
    pagination_query = pagination_params.urlencode()
    toggle_params = request.GET.copy()
    toggle_params.pop('page', None)
    if toggle_params.get('inactive') == '1':
        toggle_params.pop('inactive', None)
    else:
        toggle_params['inactive'] = '1'
    toggle_inactive_query = toggle_params.urlencode()
    sort_links = {}
    current_sort = request.GET.get('sort', 'material')
    for field in ('width', 'height', 'quantity', 'priority'):
        next_sort = field if current_sort == f'-{field}' else f'-{field}' if current_sort == field else field
        params = request.GET.copy()
        params['sort'] = next_sort
        params.pop('page', None)
        sort_links[field] = params.urlencode()
    setting = CuttingSettings.current()
    return render(request, 'cutting/inventory.html', {
        'kind': kind or 'all', 'page': page, 'materials': Material.objects.all().order_by('name'),
        'summaries': summaries, 'full_count': full_count, 'offcut_count': offcut_count,
        'total_count': total_count, 'reserved_count': reserved_count, 'available_count': available_count,
        'consumed_count': event_totals.get('consume', 0), 'scrapped_count': event_totals.get('scrap', 0),
        'pagination_query': pagination_query, 'toggle_inactive_query': toggle_inactive_query,
        'setting': setting, 'query': request.GET.get('q', ''), 'sort_links': sort_links,
        'thicknesses': Material.objects.values_list('thickness', flat=True).distinct().order_by('thickness'),
    })


@inventory_operator
def stock_create(request):
    materials = Material.objects.all().order_by('name')
    sizes = SheetSizeSpec.objects.filter(active=True).select_related('material')
    if request.method == 'POST':
        try:
            material = get_object_or_404(Material, pk=request.POST.get('material'))
            stock_type = request.POST.get('stock_type')
            if stock_type not in ('full', 'offcut'):
                raise ValueError('نوع موجودی را مشخص کنید.')
            width_raw = request.POST.get('width', '').strip()
            height_raw = request.POST.get('height', '').strip()
            dimension_mode = request.POST.get('dimension_mode') or ('custom' if stock_type == 'offcut' or width_raw or height_raw else 'standard')
            if stock_type == 'offcut':
                dimension_mode = 'custom'
            if dimension_mode == 'standard':
                if stock_type != 'full':
                    raise ValueError('برای ثبت پرت، اندازه سفارشی را وارد کنید.')
                spec_id = request.POST.get('size_spec')
                spec = SheetSizeSpec.objects.filter(pk=spec_id, material=material, active=True).first() if spec_id else None
                if not spec:
                    spec = SheetSizeSpec.objects.filter(material=material, active=True, is_default=True).first()
                if not spec:
                    raise ValueError('برای این متریال اندازه استانداردی ثبت نشده است؛ حالت اندازه سفارشی را انتخاب کنید.')
                width, height = spec.width, spec.height
            elif dimension_mode == 'custom':
                if bool(width_raw) != bool(height_raw) or not width_raw:
                    raise ValueError('برای اندازه سفارشی، عرض و طول را هر دو وارد کنید.')
                width = _dimension(width_raw, 'عرض')
                height = _dimension(height_raw, 'طول')
            else:
                raise ValueError('روش تعیین اندازه معتبر نیست.')
            quantity = int(request.POST.get('quantity', '1'))
            if quantity < 1: raise ValueError('تعداد باید دست‌کم 1 باشد.')
            is_offcut = stock_type == 'offcut'
            with transaction.atomic():
                stock = SheetStock.objects.create(
                    material=material, width=width, height=height, quantity=quantity,
                    is_offcut=is_offcut, grain=request.POST.get('grain', '').strip()[:100],
                    priority=int(request.POST.get('priority', '100')), note=request.POST.get('note', '').strip()[:255], created_by=request.user,
                )
                _event(stock=stock, event_type='receive', user=request.user, quantity_delta=quantity, note=stock.note or 'ثبت گروهی موجودی')
            messages.success(request, f'{quantity} ورق {"پرت" if is_offcut else "کامل"} با ابعاد {width}×{height} ثبت شد.')
            if is_offcut and request.POST.get('quick_repeat') == '1':
                return redirect(f"{reverse('cutting:stock_create')}?material={material.pk}&stock_type=offcut&quick_repeat=1")
            return redirect('cutting:offcuts' if is_offcut else 'cutting:full_sheets')
        except (ValueError, InvalidOperation) as exc:
            messages.error(request, str(exc))
    posted = request.POST if request.method == 'POST' else {}
    stock_type = posted.get('stock_type') or request.GET.get('stock_type', 'full')
    dimension_mode = posted.get('dimension_mode') or (
        'custom' if stock_type == 'offcut' or posted.get('width') or posted.get('height') else 'standard'
    )
    return render(request, 'cutting/stock_form.html', {
        'materials': materials, 'sizes': sizes, 'stock': None, 'posted': posted,
        'selected_material_id': posted.get('material') or request.GET.get('material', ''),
        'stock_type': stock_type, 'dimension_mode': dimension_mode,
        'quick_repeat': (posted.get('quick_repeat') == '1') if request.method == 'POST' else request.GET.get('quick_repeat') == '1',
    })


@inventory_operator
def stock_edit(request, stock_id):
    stock = get_object_or_404(SheetStock.objects.select_related('material'), pk=stock_id)
    if not stock.active or stock.quantity < 1:
        raise PermissionDenied
    if request.method == 'POST':
        try:
            with transaction.atomic():
                stock = SheetStock.objects.select_for_update().get(pk=stock_id)
                if not stock.active or stock.quantity < 1:
                    raise ValueError('موجودی غیرفعال یا تمام‌شده قابل ویرایش نیست.')
                if stock.plan_reservations.filter(outcome='reserved', plan__status='planned').exists() or _has_planned_cnc_reservation(stock.pk):
                    raise ValueError('این بچ برای برنامه برش رزرو است و تا آزادشدن رزرو قابل ویرایش نیست.')
                old = {'material_id': stock.material_id, 'material_name': stock.material.name,
                       'width': stock.width, 'height': stock.height, 'quantity': stock.quantity,
                       'priority': stock.priority, 'note': stock.note, 'grain': stock.grain}
                material = get_object_or_404(Material, pk=request.POST.get('material'))
                stock.width = _dimension(request.POST.get('width'), 'عرض')
                stock.height = _dimension(request.POST.get('height'), 'طول')
                stock.quantity = int(request.POST.get('quantity'))
                if stock.quantity < 1: raise ValueError('برای نگهداشتن رکورد، تعداد باید دست‌کم 1 باشد؛ برای خروج از انبار از غیرفعال‌سازی استفاده کنید.')
                stock.material = material
                stock.priority = int(request.POST.get('priority', stock.priority))
                stock.note = request.POST.get('note', '').strip()[:255]
                stock.grain = request.POST.get('grain', '').strip()[:100]
                changed = any((old['material_id'] != stock.material_id, old['width'] != stock.width,
                               old['height'] != stock.height, old['quantity'] != stock.quantity,
                               old['priority'] != stock.priority, old['note'] != stock.note, old['grain'] != stock.grain))
                reason = request.POST.get('change_reason', '').strip()
                if changed and not reason:
                    raise ValueError('برای ثبت تغییر موجودی، علت اصلاح را وارد کنید.')
                stock.save()
                if changed:
                    details = (f"{old['material_name']} {old['width']}×{old['height']} ({old['quantity']})"
                               f" → {stock.material.name} {stock.width}×{stock.height} ({stock.quantity}); "
                               f"اولویت {old['priority']}→{stock.priority}. علت: {reason}")
                    _event(stock=stock, event_type='adjust', user=request.user,
                           quantity_delta=stock.quantity - old['quantity'], width=old['width'], height=old['height'], note=details)
            messages.success(request, 'موجودی ویرایش شد؛ تغییر و علت آن در تاریخچه ثبت شد.')
            return redirect('cutting:offcuts' if stock.is_offcut else 'cutting:full_sheets')
        except (ValueError, InvalidOperation) as exc:
            messages.error(request, str(exc))
            stock.refresh_from_db()
    posted = request.POST if request.method == 'POST' else {}
    return render(request, 'cutting/stock_form.html', {
        'materials': Material.objects.all(), 'sizes': [], 'stock': stock, 'posted': posted,
        'selected_material_id': posted.get('material') or str(stock.material_id),
    })


@inventory_operator
@require_POST
def stock_deactivate(request, stock_id):
    reason = request.POST.get('reason', '').strip()
    stock = get_object_or_404(SheetStock, pk=stock_id)
    if not reason:
        messages.error(request, 'برای غیرفعال‌سازی، علت را وارد کنید.')
    else:
        try:
            with transaction.atomic():
                stock = SheetStock.objects.select_for_update().get(pk=stock_id)
                if stock.plan_reservations.filter(outcome='reserved', plan__status='planned').exists() or _has_planned_cnc_reservation(stock.pk):
                    raise ValueError('موجودی رزروشده قابل غیرفعال‌سازی نیست؛ ابتدا برنامه را لغو کنید.')
                stock.active = False; stock.inactive_reason = reason[:255]; stock.save(update_fields=('active', 'inactive_reason', 'updated_at'))
                _event(stock=stock, event_type='deactivate', user=request.user, note=reason)
            messages.success(request, 'موجودی غیرفعال شد؛ سابقه آن حفظ شده است.')
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('cutting:inventory')


@inventory_operator
@require_POST
def stock_reactivate(request, stock_id):
    get_object_or_404(SheetStock, pk=stock_id)
    with transaction.atomic():
        stock = SheetStock.objects.select_for_update().get(pk=stock_id)
        if stock.active:
            messages.info(request, 'این ردیف از قبل فعال است.')
        elif stock.quantity < 1:
            messages.error(request, 'موجودی با تعداد صفر قابل فعال‌سازی نیست.')
        else:
            stock.active = True; stock.inactive_reason = ''; stock.save(update_fields=('active', 'inactive_reason', 'updated_at'))
            _event(stock=stock, event_type='reactivate', user=request.user, note=request.POST.get('reason', 'فعال‌سازی مجدد'))
            messages.success(request, 'موجودی دوباره فعال شد.')
    return redirect('cutting:inventory')


@inventory_operator
def size_specs(request, spec_id=None):
    spec = get_object_or_404(SheetSizeSpec, pk=spec_id) if spec_id else None
    if request.method == 'POST':
        if spec and request.POST.get('action') == 'deactivate':
            if spec.is_default:
                spec.is_default = False
            spec.active = False
            spec.save(update_fields=('is_default', 'active'))
            messages.success(request, 'اندازه غیرفعال شد؛ موجودی‌های ثبت‌شده تغییری نکردند.')
            return redirect('cutting:size_specs')
        try:
            material = get_object_or_404(Material, pk=request.POST.get('material'))
            name = request.POST.get('name', '').strip()
            if not name: raise ValueError('عنوان اندازه لازم است.')
            duplicates = SheetSizeSpec.objects.filter(material=material, name=name)
            if spec: duplicates = duplicates.exclude(pk=spec.pk)
            if duplicates.exists(): raise ValueError('برای این متریال، عنوان اندازه تکراری است.')
            width = _dimension(request.POST.get('width'), 'عرض'); height = _dimension(request.POST.get('height'), 'طول')
            is_default = request.POST.get('is_default') == 'on'
            with transaction.atomic():
                if is_default:
                    SheetSizeSpec.objects.filter(material=material, is_default=True).exclude(pk=spec.pk if spec else None).update(is_default=False)
                if spec:
                    spec.material=material; spec.name=name; spec.width=width; spec.height=height; spec.is_default=is_default; spec.active=True; spec.save()
                else:
                    spec=SheetSizeSpec.objects.create(material=material,name=name,width=width,height=height,is_default=is_default)
            messages.success(request, 'اندازه استاندارد ذخیره شد؛ موجودی‌های قبلی بدون تغییر ماندند.')
            return redirect('cutting:size_specs')
        except (ValueError, InvalidOperation) as exc:
            messages.error(request, str(exc))
    return render(request, 'cutting/size_specs.html', {'specs': SheetSizeSpec.objects.select_related('material').all(), 'materials': Material.objects.all(), 'editing': spec})


@manager_only
def cutting_policy(request):
    setting = CuttingSettings.current()
    if request.method == 'POST' and request.POST.get('action') == 'save_kerf':
        try:
            kerf = Decimal(request.POST.get('default_kerf', ''))
            if not kerf.is_finite() or kerf < 0 or kerf > Decimal('999.9') or kerf.quantize(MM) != kerf:
                raise ValueError
            setting.default_kerf = kerf.quantize(MM)
            setting.updated_by = request.user
            setting.save(update_fields=('default_kerf', 'updated_by', 'updated_at'))
            messages.success(request, 'ضخامت تیغه‌ی پیش‌فرض ذخیره شد.')
        except (InvalidOperation, ValueError):
            messages.error(request, 'ضخامت تیغه باید عددی نامنفی با حداکثر یک رقم اعشار باشد.')
        return redirect('cutting:policy')
    return render(request, 'cutting/policy.html', {'setting': setting})
@inventory_operator
def stock_history(request):
    events = SheetStockEvent.objects.select_related('material', 'stock', 'plan', 'created_by').order_by('-created_at', '-pk')
    if request.GET.get('material'):
        events = events.filter(material_id=request.GET['material'])
    event_type = request.GET.get('event_type', '')
    if event_type in dict(SheetStockEvent.TYPES):
        events = events.filter(event_type=event_type)
    date_from_raw, date_to_raw = request.GET.get('date_from', ''), request.GET.get('date_to', '')
    date_from, date_to = _parse_history_date(date_from_raw), _parse_history_date(date_to_raw)
    if date_from_raw and date_from:
        events = events.filter(created_at__date__gte=date_from)
    elif date_from_raw:
        messages.error(request, 'تاریخ شروع معتبر نیست؛ تاریخ شمسی 1405/07/25 یا میلادی YYYY-MM-DD وارد کنید.')
    if date_to_raw and date_to:
        events = events.filter(created_at__date__lte=date_to)
    elif date_to_raw:
        messages.error(request, 'تاریخ پایان معتبر نیست؛ تاریخ شمسی 1405/07/25 یا میلادی YYYY-MM-DD وارد کنید.')
    if date_from and date_to and date_from > date_to:
        messages.error(request, 'تاریخ شروع نمی‌تواند بعد از تاریخ پایان باشد.')
        events = events.none()
    query = request.GET.get('q', '').strip()
    if query:
        search = Q(material__name__icontains=query) | Q(note__icontains=query) | Q(created_by__username__icontains=query)
        if query.isdigit():
            search |= Q(plan_id=int(query)) | Q(width=Decimal(query)) | Q(height=Decimal(query))
        events = events.filter(search)
    page = Paginator(events, 60).get_page(request.GET.get('page'))
    for event in page.object_list:
        local_datetime = timezone.localtime(event.created_at)
        event.created_at_jalali = jdatetime.datetime.fromgregorian(datetime=local_datetime).strftime('%Y/%m/%d %H:%M')
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'cutting/history.html', {
        'page': page, 'materials': Material.objects.all().order_by('name'),
        'event_types': SheetStockEvent.TYPES, 'event_type_filter': event_type,
        'query': query, 'pagination_query': params.urlencode(),
    })


def _parse_history_date(value):
    value = (value or '').strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
    if not value:
        return None
    match = re.fullmatch(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', value)
    if not match:
        return None
    year, month, day = map(int, match.groups())
    try:
        if year < 1700:
            return jdatetime.date(year, month, day).togregorian()
        return parse_date(f'{year:04d}-{month:02d}-{day:02d}')
    except (ValueError, TypeError):
        return None
