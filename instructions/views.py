"""ویوهای اپ دستورالعمل‌ها — دانش تولید: مرور، ویرایش، سند (HTML/Word) و چک‌لیست ماندگار."""
import json

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from product.models import ColorCode, Material, Order, OrderItem, Product

from . import docs, export
from .cnc_guide import get_active_setup
from .models import ChecklistState, WorkInstruction, WorkInstructionImage
from .utils import checklist_checked_lines

STATION_LABELS = docs.STATION_LABELS
DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def _is_staff(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def _can_scan(user):
    """اپراتورها (پروفایل کاری) و مدیران."""
    return user.is_authenticated and (hasattr(user, 'workerprofile') or _is_staff(user))


def _json_error(message, status=400):
    return JsonResponse({'success': False, 'error': message}, status=status)


# ── سندها ───────────────────────────────────────────────────────
@login_required
@user_passes_test(_is_staff)
def product_doc(request, pk):
    product = get_object_or_404(Product.objects.select_related('category'), pk=pk)
    context = docs.build_product_doc(product)
    context.update({
        'docx_url': reverse('instructions:product_doc_docx', args=[product.pk]),
        'edit_url': reverse('instructions:product_editor', args=[product.pk]),
    })
    return render(request, 'instructions/product_doc.html', context)


@login_required
@user_passes_test(_is_staff)
def product_doc_docx(request, pk):
    product = get_object_or_404(Product.objects.select_related('category'), pk=pk)
    buffer = export.render_product_docx(docs.build_product_doc(product), docs.parse_hidden_kinds(request))
    name = f"سند_{docs.safe_filename(product.name)}.docx"
    return FileResponse(buffer, as_attachment=True, filename=name, content_type=DOCX_MIME)


@login_required
@user_passes_test(_can_scan)
def order_doc(request, pk):
    """سند سفارش‌محور؛ فقط اپراتور/مدیر (حاوی نام مشتری است، نه هر کاربر واردشده)."""
    order = get_object_or_404(Order.objects.select_related('customer', 'user'), pk=pk)
    context = docs.build_order_doc(order)
    context['docx_url'] = reverse('instructions:order_doc_docx', args=[order.pk])
    return render(request, 'instructions/order_doc.html', context)


@login_required
@user_passes_test(_can_scan)
def order_doc_docx(request, pk):
    order = get_object_or_404(Order.objects.select_related('customer', 'user'), pk=pk)
    buffer = export.render_order_docx(docs.build_order_doc(order), docs.parse_hidden_kinds(request))
    return FileResponse(buffer, as_attachment=True, filename=f'سند_سفارش_{order.pk}.docx',
                        content_type=DOCX_MIME)


# ── مرورگر و ویرایشگر ────────────────────────────────────────────
@login_required
@user_passes_test(_is_staff)
def knowledge_browser(request):
    instructions = (WorkInstruction.objects
                    .select_related('product', 'part', 'condition_material', 'condition_color')
                    .prefetch_related('images'))
    products = (Product.objects
                .annotate(total=Count('work_instructions'),
                          active_count=Count('work_instructions', filter=Q(work_instructions__active=True)))
                .filter(total__gt=0).select_related('category'))
    stats = {
        'total': instructions.count(),
        'active': instructions.filter(active=True).count(),
        'warnings': instructions.filter(active=True, kind=WorkInstruction.KIND_WARNING).count(),
        'checklists': instructions.filter(active=True, kind=WorkInstruction.KIND_CHECKLIST).count(),
        'products': products.count(),
    }

    q = (request.GET.get('q') or '').strip()
    station = request.GET.get('station') or ''
    kind = request.GET.get('kind') or ''
    status = request.GET.get('status') or ''
    product_id = request.GET.get('product') or ''
    filtered = instructions
    if q:
        filtered = filtered.filter(Q(title__icontains=q) | Q(body__icontains=q))
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


def _group_for_editor(product):
    grouped = {code: [] for code in WorkInstruction.STATION_ORDER}
    for ins in (product.work_instructions.filter(part__isnull=True)
                .prefetch_related('images', 'condition_material', 'condition_color')
                .order_by('sequence', 'id')):
        grouped.setdefault(ins.station, []).append(ins)
    return [{'code': code, 'label': STATION_LABELS.get(code, code), 'items': grouped.get(code) or []}
            for code in WorkInstruction.STATION_ORDER]


def _serialize_for_editor(ins):
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
        'images': [{'id': img.pk, 'url': img.image.url, 'caption': img.caption}
                   for img in ins.images.all()],
    }


@login_required
@user_passes_test(_is_staff)
def product_editor(request, pk):
    product = get_object_or_404(Product.objects.select_related('category'), pk=pk)
    all_instructions = list(product.work_instructions.filter(part__isnull=True)
                            .prefetch_related('images', 'condition_material', 'condition_color'))
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


def _apply_instruction_fields(obj, request, product=None):
    obj.station = request.POST.get('station', WorkInstruction.GENERAL)
    if obj.station not in STATION_LABELS:
        return obj, 'ایستگاه نامعتبر است.'
    obj.kind = request.POST.get('kind', WorkInstruction.KIND_NOTE)
    if obj.kind not in docs.KIND_CODES:
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
    pk = request.POST.get('id', '').strip()
    if pk.isdigit():
        ins = get_object_or_404(WorkInstruction.objects.select_related('product'), pk=int(pk))
        if not ins.product_id:
            return _json_error('دستورالعمل قطعه‌محور از ادمین ویرایش می‌شود.')
        product = ins.product
    else:
        product_id = request.POST.get('product', '').strip()
        if not product_id.isdigit():
            return _json_error('محصول مشخص نیست.')
        product = get_object_or_404(Product, pk=int(product_id))
        ins = WorkInstruction()

    ins, error = _apply_instruction_fields(ins, request, product=product)
    if error:
        return _json_error(error)

    is_new = ins.pk is None
    if is_new:
        ins.created_by = request.user
        last = (WorkInstruction.objects.filter(product=product, station=ins.station, part__isnull=True)
                .order_by('-sequence').first())
        ins.sequence = (last.sequence + 10) if last else 10
    ins.updated_by = request.user
    ins.save()
    return JsonResponse({'success': True, 'instruction': _serialize_for_editor(ins), 'is_new': is_new})


@login_required
@user_passes_test(_is_staff)
@require_POST
def instruction_delete(request, pk):
    get_object_or_404(WorkInstruction, pk=pk).delete()
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
    """ذخیره‌ی ترتیب: {ids: [7, 3, 9]} به ترتیب نمایش."""
    product = get_object_or_404(Product, pk=pk)
    try:
        ids = [int(i) for i in json.loads(request.body or '{}').get('ids', [])]
    except (ValueError, TypeError):
        return _json_error('بدنه‌ی درخواست نامعتبر است.')
    owned = set(WorkInstruction.objects.filter(product=product, part__isnull=True)
                .values_list('id', flat=True))
    if any(i not in owned for i in ids):
        return _json_error('دستورالعمل خارج از این محصول در فهرست است.')
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
        return _json_error('فایل تصویر ارسال نشده است.')
    img = WorkInstructionImage(instruction=ins, image=uploaded,
                               caption=(request.POST.get('caption') or '').strip())
    last = ins.images.order_by('-sort_order').first()
    img.sort_order = (last.sort_order + 1) if last else 0
    try:
        img.full_clean()
        img.save()
    except Exception as exc:
        return _json_error(str(exc))
    return JsonResponse({'success': True, 'image': {'id': img.pk, 'url': img.image.url, 'caption': img.caption}})


@login_required
@user_passes_test(_is_staff)
@require_POST
def instruction_image_delete(request, pk):
    get_object_or_404(WorkInstructionImage, pk=pk).delete()
    return JsonResponse({'success': True})


# ── چک‌لیست ماندگار ──────────────────────────────────────────────
@login_required
@require_POST
def checklist_toggle(request):
    if not _can_scan(request.user):
        return _json_error('دسترسی ندارید.', 403)
    try:
        payload = json.loads(request.body or '{}')
        instruction_id = int(payload.get('instruction_id'))
        order_item_id = int(payload.get('order_item_id'))
        line_index = int(payload.get('line_index'))
        checked = bool(payload.get('checked'))
    except (ValueError, TypeError):
        return _json_error('بدنه‌ی درخواست نامعتبر است.')

    ins = get_object_or_404(WorkInstruction, pk=instruction_id)
    if ins.kind != WorkInstruction.KIND_CHECKLIST:
        return _json_error('این بند چک‌لیست نیست.')
    if line_index < 0 or line_index >= len(ins.body_lines()):
        return _json_error('شماره‌ی خط با متن فعلی هم‌خوانی ندارد.')
    item = get_object_or_404(OrderItem, pk=order_item_id)

    if checked:
        ChecklistState.objects.get_or_create(
            instruction=ins, order_item=item, line_index=line_index,
            defaults={'checked_by': request.user})
    else:
        ChecklistState.objects.filter(instruction=ins, order_item=item, line_index=line_index).delete()
    return JsonResponse({'success': True, 'checked': checked})


@login_required
def checklist_state(request, item_id):
    if not _can_scan(request.user):
        return _json_error('دسترسی ندارید.', 403)
    item = get_object_or_404(OrderItem, pk=item_id)
    checked = checklist_checked_lines(item)
    return JsonResponse({'success': True, 'checked': {str(k): sorted(v) for k, v in checked.items()}})
