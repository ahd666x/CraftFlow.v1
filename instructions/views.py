"""ویوهای اپ دستورالعمل‌ها — دانش تولید: مرور، ویرایش، سند و چک‌لیست ماندگار.

فاز ۲: بخش خودکار ایستگاه CNC (پارکینگ ابزار، نقشه‌ی میز با نقاط فیکس،
روند آفست) از راه‌اندازی ماشین به سند اضافه می‌شود.
فاز ۳: مرورگر دانش، ویرایشگر اختصاصی محصول (درگ‌سورت و عکس با AJAX)،
سند سفارش‌محور با اعمال شرط متریال/رنگ واقعی و چک‌لیست ماندگار.
"""
import json

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from product.models import ColorCode, Material, Order, Product

from .cnc_guide import get_active_setup, serialize_setup
from .models import ChecklistState, WorkInstruction
from .utils import (
    _item_color_codes,
    _item_material_ids,
    checklist_checked_lines,
    models_q_for_item,
)


def _is_staff(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def _can_scan(user):
    """اپراتورها (پروفایل کاری) و مدیران — برای تیک چک‌لیست ماندگار."""
    return user.is_authenticated and (hasattr(user, 'workerprofile') or _is_staff(user))


@login_required
@user_passes_test(_is_staff)
def product_doc(request, pk):
    """سند گردآوری‌شده‌ی دستور تولید محصول: ایستگاه‌ها به ترتیب مسیر تولید.

    این همان قالب عمومی محصول است؛ نسخه‌ی «به ازای سفارش» (با متریال و رنگ
    واقعی و اعمال شرط‌ها) در فاز بعدی از همین ساختار استفاده می‌کند.
    در قالب محصول همه‌ی دستورالعمل‌های فعال نشان داده می‌شوند و شرط‌های
    متریال/رنگ به‌صورت برچسب کنار هر بند مشخص‌اند.
    """
    product = get_object_or_404(
        Product.objects.select_related('category'), pk=pk,
    )
    instructions = (WorkInstruction.objects
                    .active()
                    .filter(product=product)
                    .prefetch_related('images', 'condition_material', 'condition_color'))
    cnc_setup = _cnc_doc_block()

    grouped = {code: [] for code in WorkInstruction.STATION_ORDER}
    for ins in instructions:
        grouped.setdefault(ins.station, []).append(ins)

    sections = []
    for code in WorkInstruction.STATION_ORDER:
        items = grouped.get(code) or []
        auto = (code == 'cnc' and cnc_setup is not None)
        if not items and not auto:
            continue
        sections.append({
            'code': code,
            'label': dict(WorkInstruction.STATION_FILTER_CHOICES).get(code, code),
            'items': items,
        })

    bom_rows = []
    for row in product.bom.select_related('part__material').order_by('id'):
        part = row.part
        bom_rows.append({
            'name': part.name or part.f2 or part.f3 or '—',
            'material': part.material.name if part.material_id else '—',
            'size': f"{part.length}×{part.width}",
            'edges': [(label, getattr(part, field) or '—')
                      for label, field in (('F26', 'f26'), ('F18', 'f18'), ('F4', 'f4'), ('F5', 'f5'))],
            'routing': part.routing_code,
            'quantity': row.quantity,
        })

    context = {
        'product': product,
        'sections': sections,
        'bom_rows': bom_rows,
        'has_instructions': bool(sections),
        'cnc_setup': cnc_setup,
    }
    return render(request, 'instructions/product_doc.html', context)


def _cnc_doc_block():
    """بخش خودکار ایستگاه CNC برای سند چاپی؛ None یعنی راه‌اندازی ثبت نشده."""
    setup = get_active_setup()
    if setup is None:
        return None
    payload = serialize_setup(setup)
    tw, th = float(setup.table_width), float(setup.table_height)
    aspect = th / tw if tw else 1

    def place(x, y):
        return {
            'left': round(x / tw * 100, 2) if tw else 0,
            'top': round((th - y) / th * 100, 2) if th else 0,
        }

    points = []
    for point in payload['points']:
        pos = place(point['x'], point['y'])
        points.append({
            'label': point['label'],
            'kind_label': point['kind_label'],
            'left': pos['left'], 'top': pos['top'],
        })
    origin = place(setup.origin_x, setup.origin_y)
    payload['map'] = {'points': points, 'origin': origin, 'aspect': round(aspect, 4)}
    return payload


# ترتیب ایستگاه‌ها در ویرایشگر: همان مسیر تولید + ارسال در انتها.
EDITOR_STATION_ORDER = WorkInstruction.STATION_ORDER + ['shipping']
STATION_LABELS = dict(WorkInstruction.STATION_FILTER_CHOICES)


def _group_for_editor(product):
    """دستورالعمل‌های سطح محصول گروه‌بندی‌شده به تفکیک ایستگاه برای ویرایشگر."""
    instructions = (product.work_instructions
                    .filter(part__isnull=True)
                    .prefetch_related('images', 'condition_material', 'condition_color')
                    .order_by('sequence', 'id'))
    grouped = {code: [] for code in EDITOR_STATION_ORDER}
    for ins in instructions:
        grouped.setdefault(ins.station, []).append(ins)
    return [
        {'code': code, 'label': STATION_LABELS.get(code, code), 'items': grouped.get(code) or []}
        for code in EDITOR_STATION_ORDER
    ]


@login_required
@user_passes_test(_is_staff)
def knowledge_browser(request):
    """مرورگر دانش تولید: آمار کلی + کارت محصولات + جدول کل دستورالعمل‌ها."""
    from django.db.models import Count, Q

    instructions = (WorkInstruction.objects
                    .select_related('product', 'part', 'condition_material', 'condition_color')
                    .prefetch_related('images'))

    products = (Product.objects
                .annotate(
                    total=Count('work_instructions'),
                    active_count=Count('work_instructions', filter=Q(work_instructions__active=True)),
                )
                .filter(total__gt=0)
                .select_related('category'))

    stats = {
        'total': instructions.count(),
        'active': instructions.filter(active=True).count(),
        'warnings': instructions.filter(active=True, kind=WorkInstruction.KIND_WARNING).count(),
        'checklists': instructions.filter(active=True, kind=WorkInstruction.KIND_CHECKLIST).count(),
        'products': products.count(),
    }

    # فیلترهای جدول
    q = (request.GET.get('q') or '').strip()
    station = request.GET.get('station') or ''
    kind = request.GET.get('kind') or ''
    status = request.GET.get('status') or ''
    product_id = request.GET.get('product') or ''
    filtered = instructions
    if q:
        from django.db.models import Q as _Q
        filtered = filtered.filter(_Q(title__icontains=q) | _Q(body__icontains=q))
    if station:
        filtered = filtered.filter(station=station)
    if kind:
        filtered = filtered.filter(kind=kind)
    if status == 'active':
        filtered = filtered.filter(active=True)
    elif status == 'inactive':
        filtered = filtered.filter(active=False)
    if product_id.isdigit():
        filtered = filtered.filter(product_id=int(product_id), part__isnull=True)

    context = {
        'stats': stats,
        'products': products,
        'rows': filtered.order_by('station', 'sequence', 'id')[:400],
        'stations': WorkInstruction.STATION_FILTER_CHOICES,
        'kinds': WorkInstruction.KIND_CHOICES,
        'filters': {'q': q, 'station': station, 'kind': kind, 'status': status, 'product': product_id},
        'cnc_setup_active': get_active_setup() is not None,
    }
    return render(request, 'instructions/browser.html', context)


@login_required
@user_passes_test(_is_staff)
def product_editor(request, pk):
    """ویرایشگر اختصاصی دستورالعمل‌های محصول: گروه‌بندی ایستگاهی + درگ‌سورت + عکس."""
    product = get_object_or_404(Product.objects.select_related('category'), pk=pk)
    all_instructions = list(
        product.work_instructions
        .filter(part__isnull=True)
        .prefetch_related('images', 'condition_material', 'condition_color')
    )
    context = {
        'product': product,
        'sections': _group_for_editor(product),
        'card_data': [_serialize_for_editor(ins) for ins in all_instructions],
        'materials': Material.objects.order_by('name'),
        'colors': ColorCode.objects.filter(is_active=True).order_by('code'),
        'kinds': WorkInstruction.KIND_CHOICES,
        'stations': WorkInstruction.STATION_FILTER_CHOICES,
        'cnc_setup_active': get_active_setup() is not None,
    }
    return render(request, 'instructions/editor.html', context)


def _serialize_for_editor(ins):
    """سریال کامل یک دستورالعمل برای بازگشت AJAX ویرایشگر."""
    return {
        'id': ins.pk,
        'station': ins.station,
        'station_label': STATION_LABELS.get(ins.station, ins.station),
        'kind': ins.kind,
        'kind_label': ins.get_kind_display(),
        'badge': list(ins.badge),
        'title': ins.title,
        'body': ins.body or '',
        'condition_material': ins.condition_material_id,
        'condition_color': ins.condition_color_id,
        'condition_labels': ins.condition_labels,
        'active': ins.active,
        'images': [
            {'id': img.pk, 'url': img.image.url, 'caption': img.caption}
            for img in ins.images.all()
        ],
    }


def _apply_instruction_fields(obj, request, product=None):
    """پرکردن فیلدهای فرم از POST با اعتبارسنجی؛ برگشت (obj, error_str)."""
    obj.station = request.POST.get('station', WorkInstruction.GENERAL)
    if obj.station not in dict(WorkInstruction.STATION_FILTER_CHOICES):
        return obj, 'ایستگاه نامعتبر است.'
    obj.kind = request.POST.get('kind', WorkInstruction.KIND_NOTE)
    if obj.kind not in dict(WorkInstruction.KIND_CHOICES):
        return obj, 'نوع نامعتبر است.'
    obj.title = (request.POST.get('title') or '').strip()
    if not obj.title:
        return obj, 'عنوان الزامی است.'
    obj.body = request.POST.get('body') or ''
    obj.active = request.POST.get('active') == '1'

    material_id = (request.POST.get('condition_material') or '').strip()
    obj.condition_material_id = int(material_id) if material_id.isdigit() else None
    color_id = (request.POST.get('condition_color') or '').strip()
    obj.condition_color_id = int(color_id) if color_id.isdigit() else None

    if product is not None:
        obj.product = product
        obj.part = None
    try:
        obj.full_clean()
    except Exception as exc:
        messages = getattr(exc, 'message_dict', None)
        if messages:
            flat = []
            for msgs in messages.values():
                flat.extend(msgs if isinstance(msgs, list) else [msgs])
            return obj, ' '.join(flat)
        return obj, str(exc)
    return obj, None


@login_required
@user_passes_test(_is_staff)
@require_POST
def instruction_save(request):
    """ایجاد/ویرایش AJAX دستورالعمل سطح محصول (بدون عکس؛ عکس جداگانه)."""
    pk = request.POST.get('id', '').strip()
    if pk.isdigit():
        ins = get_object_or_404(WorkInstruction.objects.select_related('product'), pk=int(pk))
        if not ins.product_id:
            return JsonResponse({'success': False, 'error': 'دستورالعمل قطعه‌محور از ادمین ویرایش می‌شود.'}, status=400)
        product = ins.product
    else:
        product_id = request.POST.get('product', '').strip()
        if not product_id.isdigit():
            return JsonResponse({'success': False, 'error': 'محصول مشخص نیست.'}, status=400)
        product = get_object_or_404(Product, pk=int(product_id))
        ins = WorkInstruction()

    ins, error = _apply_instruction_fields(ins, request, product=product)
    if error:
        return JsonResponse({'success': False, 'error': error}, status=400)

    is_new = ins.pk is None
    if is_new:
        ins.created_by = request.user
    ins.updated_by = request.user
    # ترتیب بند جدید: انتهای همان ایستگاه
    if is_new:
        last = (WorkInstruction.objects
                .filter(product=product, station=ins.station, part__isnull=True)
                .order_by('-sequence').first())
        ins.sequence = (last.sequence + 10) if last else 10
    ins.save()
    return JsonResponse({'success': True, 'instruction': _serialize_for_editor(ins), 'is_new': is_new})


@login_required
@user_passes_test(_is_staff)
@require_POST
def instruction_delete(request, pk):
    ins = get_object_or_404(WorkInstruction, pk=pk)
    ins.delete()
    return JsonResponse({'success': True})


@login_required
@user_passes_test(_is_staff)
@require_POST
def instruction_toggle(request, pk):
    ins = get_object_or_404(WorkInstruction, pk=pk)
    ins.active = not ins.active
    ins.updated_by = request.user
    ins.save(update_fields=['active', 'updated_by', 'updated_at'])
    return JsonResponse({'success': True, 'active': ins.active})


@login_required
@user_passes_test(_is_staff)
@require_POST
def product_reorder(request, pk):
    """ذخیره‌ی ترتیب جدید بندهای یک محصول: {ids: [7, 3, 9]} به ترتیب نمایش."""
    product = get_object_or_404(Product, pk=pk)
    try:
        payload = json.loads(request.body or '{}')
        ids = [int(i) for i in payload.get('ids', [])]
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'بدنه‌ی درخواست نامعتبر است.'}, status=400)
    owned = set(WorkInstruction.objects
                .filter(product=product, part__isnull=True)
                .values_list('id', flat=True))
    if any(i not in owned for i in ids):
        return JsonResponse({'success': False, 'error': 'دستورالعمل خارج از این محصول در فهرست است.'}, status=400)
    with transaction.atomic():
        for index, ins_id in enumerate(ids):
            WorkInstruction.objects.filter(pk=ins_id).update(sequence=(index + 1) * 10)
    return JsonResponse({'success': True})


@login_required
@user_passes_test(_is_staff)
@require_POST
def instruction_image_add(request, pk):
    ins = get_object_or_404(WorkInstruction, pk=pk)
    uploaded = request.FILES.get('image')
    if not uploaded:
        return JsonResponse({'success': False, 'error': 'فایل تصویر ارسال نشده است.'}, status=400)
    from .models import WorkInstructionImage
    img = WorkInstructionImage(instruction=ins, image=uploaded,
                               caption=(request.POST.get('caption') or '').strip())
    last = ins.images.order_by('-sort_order').first()
    img.sort_order = (last.sort_order + 1) if last else 0
    try:
        img.full_clean()
        img.save()
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    return JsonResponse({'success': True, 'image': {'id': img.pk, 'url': img.image.url, 'caption': img.caption}})


@login_required
@user_passes_test(_is_staff)
@require_POST
def instruction_image_delete(request, pk):
    from .models import WorkInstructionImage
    img = get_object_or_404(WorkInstructionImage, pk=pk)
    img.delete()
    return JsonResponse({'success': True})


@login_required
@require_POST
def checklist_toggle(request):
    """تیک/حذف تیک ماندگار یک خط چک‌لیست برای آیتم سفارش."""
    if not _can_scan(request.user):
        return JsonResponse({'success': False, 'error': 'دسترسی ندارید.'}, status=403)
    try:
        payload = json.loads(request.body or '{}')
        instruction_id = int(payload.get('instruction_id'))
        order_item_id = int(payload.get('order_item_id'))
        line_index = int(payload.get('line_index'))
        checked = bool(payload.get('checked'))
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'بدنه‌ی درخواست نامعتبر است.'}, status=400)

    from product.models import OrderItem

    ins = get_object_or_404(WorkInstruction, pk=instruction_id)
    if ins.kind != WorkInstruction.KIND_CHECKLIST:
        return JsonResponse({'success': False, 'error': 'این بند چک‌لیست نیست.'}, status=400)
    lines = ins.body_lines()
    if line_index < 0 or line_index >= len(lines):
        return JsonResponse({'success': False, 'error': 'شماره‌ی خط با متن فعلی هم‌خوانی ندارد.'}, status=400)
    item = get_object_or_404(OrderItem, pk=order_item_id)

    if checked:
        ChecklistState.objects.get_or_create(
            instruction=ins, order_item=item, line_index=line_index,
            defaults={'checked_by': request.user},
        )
    else:
        ChecklistState.objects.filter(
            instruction=ins, order_item=item, line_index=line_index,
        ).delete()
    return JsonResponse({'success': True, 'checked': checked})


@login_required
def checklist_state(request, item_id):
    """وضعیت تیک‌های ماندگار آیتم: {«<instruction_id>»: [خط‌ها]}."""
    if not _can_scan(request.user):
        return JsonResponse({'success': False, 'error': 'دسترسی ندارید.'}, status=403)
    from product.models import OrderItem

    item = get_object_or_404(OrderItem, pk=item_id)
    checked = checklist_checked_lines(item)
    return JsonResponse({'success': True, 'checked': {str(k): sorted(v) for k, v in checked.items()}})


@login_required
def order_doc(request, pk):
    """سند دستور تولید سفارش‌محور: شرط متریال/رنگ واقعی هر آیتم اعمال می‌شود.

    برای هر آیتم سفارش، متریال قطعات تسک‌ها (یا BOM) و کدهای رنگ واقعی
    گرفته می‌شود و فقط دستورالعمل‌هایی که شرط‌شان می‌خواند نمایش داده
    می‌شوند؛ دستورالعمل‌های قطعه‌محور هم برای قطعات همین آیتم می‌آیند.
    راه‌اندازی ماشین CNC چون مشترک همه‌ی آیتم‌هاست یک‌بار بالای سند می‌آید.
    """
    order = get_object_or_404(
        Order.objects.select_related('customer', 'user'), pk=pk,
    )
    cnc_setup = _cnc_doc_block()

    blocks = []
    for item in order.items.select_related('product', 'product__category').order_by('id'):
        product = item.product
        instructions = (WorkInstruction.objects
                        .active()
                        .applicable_to(
                            material_ids=_item_material_ids(item),
                            color_codes=_item_color_codes(item),
                        )
                        .filter(models_q_for_item(item))
                        .select_related('part')
                        .prefetch_related('images', 'condition_material', 'condition_color'))
        grouped = {code: [] for code in WorkInstruction.STATION_ORDER}
        for ins in instructions:
            grouped.setdefault(ins.station, []).append(ins)
        sections = []
        for code in WorkInstruction.STATION_ORDER:
            items_ = grouped.get(code) or []
            if not items_:
                continue
            sections.append({'code': code, 'label': STATION_LABELS.get(code, code), 'items': items_})

        material_ids = _item_material_ids(item)
        materials = list(Material.objects.filter(id__in=material_ids).values_list('name', flat=True))
        colors = _item_color_codes(item)
        blocks.append({
            'item': item,
            'product': product,
            'sections': sections,
            'materials': materials,
            'colors': colors,
            'has_content': bool(sections),
        })

    context = {
        'order': order,
        'blocks': blocks,
        'cnc_setup': cnc_setup,
    }
    return render(request, 'instructions/order_doc.html', context)
