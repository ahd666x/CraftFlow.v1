from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from product.models import Material, Order, ProductionTask
from .models import (
    CuttingSettings, CutPlan, CutPlanSheet, CutPlanTask, SheetSizeSpec,
    SheetStock, SheetStockEvent,
)
from .optimizer import expected_offcuts, make_autocut, optimize


MM = Decimal('0.1')


def _role(user, names):
    return user.is_superuser or user.is_staff or user.groups.filter(name__in=names).exists()


def cutting_operator(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not _role(request.user, ('1', '2', '3', 'انبار', 'نماینده')):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def inventory_operator(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not (request.user.is_superuser or request.user.groups.filter(name__in=('2', '3', 'انبار')).exists()):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def manager_only(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not (request.user.is_superuser or request.user.groups.filter(name__in=('2', '3')).exists()):
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
    return result


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
        try:
            kerf = Decimal(request.POST.get('kerf', '4'))
            if not kerf.is_finite() or kerf < 0 or kerf.quantize(MM) != kerf:
                raise ValueError('ضخامت تیغه باید صفر یا عدد مثبت با حداکثر یک رقم اعشار باشد.')
        except (InvalidOperation, ValueError):
            messages.error(request, 'مقدار تیغه معتبر نیست.'); return redirect('cutting:planner')
        if not order_ids:
            messages.error(request, 'برای برنامه‌ریزی دست‌کم یک سفارش انتخاب کنید.'); return redirect('cutting:planner')
        try:
            with transaction.atomic():
                tasks = list(ProductionTask.objects.select_for_update().filter(
                    order_id__in=order_ids, station_name='cut', status='pending', part__isnull=False,
                    quantity__gt=F('completed_quantity'),
                ).select_related('part__material', 'order', 'order__customer').order_by('pk'))
                if not tasks:
                    raise ValueError('قطعه آماده برش برای سفارش‌های انتخاب‌شده پیدا نشد.')
                task_ids = [task.pk for task in tasks]
                if CutPlanTask.objects.filter(task_id__in=task_ids, plan__status='planned').exists():
                    raise ValueError('برخی از تسک‌ها هم‌اکنون در برنامه برش دیگری رزرو هستند.')
                settings_row = CuttingSettings.current()
                stock_qs = SheetStock.objects.select_for_update().filter(active=True, quantity__gt=0, material_id__in={t.part.material_id for t in tasks})
                if settings_row.strategy == 'full_only':
                    stock_qs = stock_qs.filter(is_offcut=False)
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
                planned_quantities = {}
                for task in tasks:
                    part = task.part
                    if part.length <= 0 or part.width <= 0:
                        raise ValueError(f'ابعاد قطعه «{part.name}» باید مثبت باشند.')
                    remaining_quantity = task.quantity - task.completed_quantity
                    planned_quantities[task.pk] = remaining_quantity
                    for _ in range(remaining_quantity):
                        grouped[part.material_id].append({
                            'width': part.length, 'height': part.width,
                            'orig_width': part.length, 'orig_height': part.width,
                            'turn': part.turn, 'grain': part.grain, 'f2': part.f2 or part.name,
                            'f3': part.f3, 'routing': part.routing_code, 'order': str(task.order_id),
                            'customer': str(task.order.customer), 'label': f'{part.name} / سفارش {task.order_id}',
                            'uid': len(grouped[part.material_id]), 'task_id': task.pk,
                        })
                rows = []
                try:
                    for material_id, parts in grouped.items():
                        material = next(task.part.material for task in tasks if task.part.material_id == material_id)
                        material_stock = [item for item in available if stock_by_id[item['stock_id']].material_id == material_id]
                        if not material_stock:
                            raise ValueError(f'برای «{material.name}» موجودی آزاد ثبت نشده است؛ یا تمام موجودی رزرو شده است.')
                        layouts = optimize(parts, material_stock, kerf, policy=settings_row.strategy)
                        for layout in layouts:
                            layout['expected_offcuts'] = expected_offcuts(layout, kerf)
                        rows.append({
                            'type': material.name.replace(' ', '-'), 'thickness': str(material.thickness),
                            'sheets': layouts, 'shapes': parts, 'material_id': material_id,
                        })
                except ValueError:
                    raise
                orders = list(Order.objects.filter(pk__in=order_ids))
                plan = CutPlan.objects.create(created_by=request.user, plan_data={'rows': [], 'kerf': str(kerf), 'policy': settings_row.strategy})
                plan.orders.set(orders)
                for task in tasks:
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
                            was_offcut=source.is_offcut,
                        )
                        stock_data['plan_sheet_id'] = sheet_record.pk
                        _event(stock=source, event_type='reserve', user=request.user, plan=plan, note=f'رزرو در برنامه #{plan.pk}')
                plan.plan_data = _json_safe({'rows': rows, 'kerf': kerf, 'policy': settings_row.strategy})
                plan.save(update_fields=('plan_data',))
            return redirect('cutting:plan_detail', plan_id=plan.pk)
        except ValueError as exc:
            messages.error(request, str(exc)); return redirect('cutting:planner')
    available_order_ids = CutPlanTask.objects.filter(plan__status='planned').values('task_id')
    orders = Order.objects.filter(tasks__station_name='cut', tasks__status='pending').exclude(tasks__pk__in=available_order_ids).distinct().order_by('-id')
    setting = CuttingSettings.current()
    return render(request, 'cutting/planner.html', {'materials': materials, 'orders': orders, 'setting': setting})


@cutting_operator
def plan_detail(request, plan_id):
    plan = get_object_or_404(CutPlan.objects.prefetch_related('orders', 'planned_sheets__material', 'planned_sheets__source_stock'), pk=plan_id)
    summaries = []
    planned_by_id = {s.pk: s for s in plan.planned_sheets.all()}
    for row in plan.plan_data.get('rows', []):
        for layout in row['sheets']:
            layout['sheet_record'] = planned_by_id.get(layout['sheet'].get('plan_sheet_id'))
        summaries.append({'material': row['type'], 'thickness': row['thickness'], 'sheet_count': len(row['sheets']), 'part_count': len(row['shapes']), 'sheets': row['sheets']})
    unfinished_tasks = [row for row in plan.planned_tasks.select_related('task', 'task__part') if row.task.status != 'done']
    return render(request, 'cutting/plan_detail.html', {'plan': plan, 'summaries': summaries, 'unfinished_tasks': unfinished_tasks})


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
    for index in range(1, 5):
        raw_w = request.POST.get(f'rem_{sheet.pk}_{index}_width', '').strip()
        raw_h = request.POST.get(f'rem_{sheet.pk}_{index}_height', '').strip()
        if not raw_w and not raw_h:
            continue
        if not raw_w or not raw_h:
            raise ValueError('برای هر پرت واقعی باید هم عرض و هم طول وارد شود.')
        width = _dimension(raw_w, 'عرض پرت')
        height = _dimension(raw_h, 'طول پرت')
        if width > sheet.width or height > sheet.height:
            raise ValueError('ابعاد پرت نمی‌تواند از ابعاد ورق اولیه بزرگ‌تر باشد.')
        remnants.append({'width': format(width, 'f'), 'height': format(height, 'f')})
    if sum(Decimal(item['width']) * Decimal(item['height']) for item in remnants) > sheet.width * sheet.height:
        raise ValueError('مجموع مساحت پرت‌های واردشده از مساحت ورق مبدأ بیشتر است.')
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
            sheets = list(CutPlanSheet.objects.select_for_update().filter(plan=plan).select_related('source_stock', 'material').order_by('pk'))
            task_rows = list(CutPlanTask.objects.select_for_update().filter(plan=plan).select_related('task'))
            if not sheets or not task_rows:
                raise ValueError('این برنامه قدیمی یا ناقص است؛ برای ایمنی موجودی، برنامه را دوباره ایجاد کنید.')
            if any(row.task.status != 'pending' or row.task.quantity != row.task_quantity or row.task.completed_quantity != row.completed_before for row in task_rows):
                raise ValueError('تسک‌های برنامه از زمان ایجاد تغییر کرده‌اند؛ برنامه را دوباره بسازید.')
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
            for data_row in plan.plan_data.get('rows', []):
                for layout in data_row.get('sheets', []):
                    sheet_id = int(layout['sheet']['plan_sheet_id'])
                    if results[sheet_id][0] != 'used':
                        continue
                    for piece in layout.get('parts', []):
                        if piece.get('task_id') is not None:
                            cut_piece_counts[int(piece['task_id'])] += 1
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
            sheet.outcome = 'untouched'; sheet.save(update_fields=('outcome',))
        plan.status = 'cancelled'; plan.cancelled_at = timezone.now(); plan.save(update_fields=('status', 'cancelled_at'))
    messages.success(request, 'برنامه لغو و رزروها آزاد شدند؛ موجودی واقعی تغییری نکرد.')
    return redirect('cutting:plan_detail', plan_id=plan.pk)


def _inventory_queryset(request, kind=None):
    inactive = request.GET.get('inactive') == '1'
    qs = SheetStock.objects.filter(active=False) if inactive else SheetStock.objects.filter(active=True, quantity__gt=0)
    qs = qs.select_related('material')
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
    sort_map = {'material': ('material__name', 'width', 'height', 'pk'), 'width': ('width', 'height', 'pk'), 'height': ('height', 'width', 'pk'), 'quantity': ('quantity', 'pk'), 'priority': ('priority', 'pk'), 'date': ('-created_at', '-pk')}
    sort = request.GET.get('sort', 'material')
    if sort == '-quantity': qs = qs.order_by('-quantity', 'pk')
    else: qs = qs.order_by(*sort_map.get(sort, sort_map['material']))
    return qs


@inventory_operator
def inventory(request, kind=None):
    active = SheetStock.objects.filter(active=True, quantity__gt=0)
    full_count = active.filter(is_offcut=False).aggregate(total=Sum('quantity'))['total'] or 0
    offcut_count = active.filter(is_offcut=True).aggregate(total=Sum('quantity'))['total'] or 0
    summaries = list(active.values('material__name', 'material__thickness', 'is_offcut').annotate(units=Sum('quantity')).order_by('material__name', 'is_offcut'))
    rows = _inventory_queryset(request, kind)
    page = Paginator(rows, 40).get_page(request.GET.get('page'))
    setting = CuttingSettings.current()
    return render(request, 'cutting/inventory.html', {
        'kind': kind or 'all', 'page': page, 'materials': Material.objects.all().order_by('name'),
        'summaries': summaries, 'full_count': full_count, 'offcut_count': offcut_count,
        'setting': setting, 'query': request.GET.get('q', ''),
        'thicknesses': Material.objects.values_list('thickness', flat=True).distinct().order_by('thickness'),
    })


@inventory_operator
def stock_create(request):
    materials = Material.objects.all().order_by('name')
    sizes = SheetSizeSpec.objects.filter(active=True).select_related('material')
    if request.method == 'POST':
        try:
            material = get_object_or_404(Material, pk=request.POST.get('material'))
            if request.POST.get('stock_type') not in ('full', 'offcut'):
                raise ValueError('نوع موجودی را مشخص کنید.')
            spec_id = request.POST.get('size_spec')
            spec = SheetSizeSpec.objects.filter(pk=spec_id, material=material, active=True).first() if spec_id else None
            if not spec and request.POST.get('stock_type') != 'offcut':
                spec = SheetSizeSpec.objects.filter(material=material, active=True, is_default=True).first()
            width_raw = request.POST.get('width', '').strip()
            height_raw = request.POST.get('height', '').strip()
            if bool(width_raw) != bool(height_raw):
                raise ValueError('برای اندازه سفارشی، عرض و طول را هر دو وارد کنید.')
            width = _dimension(width_raw, 'عرض') if width_raw else (spec.width if spec else None)
            height = _dimension(height_raw, 'طول') if height_raw else (spec.height if spec else None)
            if width is None or height is None: raise ValueError('اندازه استاندارد انتخاب کنید یا ابعاد سفارشی را وارد کنید.')
            quantity = int(request.POST.get('quantity', '1'))
            if quantity < 1: raise ValueError('تعداد باید دست‌کم ۱ باشد.')
            is_offcut = request.POST.get('stock_type') == 'offcut'
            with transaction.atomic():
                stock = SheetStock.objects.create(
                    material=material, width=width, height=height, quantity=quantity,
                    is_offcut=is_offcut, grain=request.POST.get('grain', '').strip()[:100],
                    priority=int(request.POST.get('priority', '100')), note=request.POST.get('note', '').strip()[:255], created_by=request.user,
                )
                _event(stock=stock, event_type='receive', user=request.user, quantity_delta=quantity, note=stock.note or 'ثبت گروهی موجودی')
            messages.success(request, f'{quantity} ورق {"پرت" if is_offcut else "کامل"} با ابعاد {width}×{height} ثبت شد.')
            return redirect('cutting:offcuts' if is_offcut else 'cutting:full_sheets')
        except (ValueError, InvalidOperation) as exc:
            messages.error(request, str(exc))
    return render(request, 'cutting/stock_form.html', {'materials': materials, 'sizes': sizes, 'stock': None})


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
                if stock.plan_reservations.filter(outcome='reserved', plan__status='planned').exists():
                    raise ValueError('این بچ برای برنامه برش رزرو است و تا آزادشدن رزرو قابل ویرایش نیست.')
                old = {'material_id': stock.material_id, 'material_name': stock.material.name,
                       'width': stock.width, 'height': stock.height, 'quantity': stock.quantity,
                       'priority': stock.priority, 'note': stock.note, 'grain': stock.grain}
                material = get_object_or_404(Material, pk=request.POST.get('material'))
                stock.width = _dimension(request.POST.get('width'), 'عرض')
                stock.height = _dimension(request.POST.get('height'), 'طول')
                stock.quantity = int(request.POST.get('quantity'))
                if stock.quantity < 1: raise ValueError('برای نگهداشتن رکورد، تعداد باید دست‌کم ۱ باشد؛ برای خروج از انبار از غیرفعال‌سازی استفاده کنید.')
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
    return render(request, 'cutting/stock_form.html', {'materials': Material.objects.all(), 'sizes': [], 'stock': stock})


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
                if stock.plan_reservations.filter(outcome='reserved', plan__status='planned').exists():
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
    if request.method == 'POST':
        if request.POST.get('stock_id'):
            stock = get_object_or_404(SheetStock, pk=request.POST.get('stock_id'), active=True)
            try:
                priority = int(request.POST.get('priority'))
                old_priority = stock.priority
                stock.priority = priority
                stock.save(update_fields=('priority', 'updated_at'))
                _event(stock=stock, event_type='adjust', user=request.user,
                       note=f'تغییر اولویت مصرف از {old_priority} به {priority}')
                messages.success(request, 'اولویت این ردیف موجودی به‌روزرسانی شد.')
            except (TypeError, ValueError):
                messages.error(request, 'اولویت باید عدد صحیح باشد.')
            return redirect('cutting:policy')
        strategy = request.POST.get('strategy')
        if strategy not in dict(CuttingSettings.STRATEGIES):
            messages.error(request, 'سیاست مصرف انتخاب‌شده معتبر نیست.')
        else:
            setting.strategy=strategy; setting.updated_by=request.user; setting.save()
            messages.success(request, 'سیاست مصرف ورق ذخیره شد؛ پرت‌های انبار حذف یا غیرفعال نشدند.')
            return redirect('cutting:policy')
    stock = SheetStock.objects.filter(active=True, quantity__gt=0).select_related('material').order_by('priority', 'is_offcut', 'material__name', 'width', 'height', 'pk')
    return render(request, 'cutting/policy.html', {'setting': setting, 'strategies': CuttingSettings.STRATEGIES, 'stock': stock})


@inventory_operator
def stock_history(request):
    events = SheetStockEvent.objects.select_related('material', 'stock', 'plan', 'created_by')
    if request.GET.get('material'):
        events = events.filter(material_id=request.GET['material'])
    page = Paginator(events, 60).get_page(request.GET.get('page'))
    return render(request, 'cutting/history.html', {'page': page, 'materials': Material.objects.all().order_by('name')})
