import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Q, Sum, Count, F, Case, When, Value, CharField, ProtectedError, DecimalField
from django.db.models.functions import Coalesce
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render, redirect
from django.template.loader import render_to_string
from django.db.transaction import atomic
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from product.decorators import admin_or_manager_required
from .models import (
    Supplier, RawMaterialCategory, RawMaterial,
    StockMovement, PurchaseOrder, PurchaseOrderItem
    , MaterialIssue
)
from .forms import (
    SupplierForm, RawMaterialCategoryForm, RawMaterialForm,
    StockMovementForm, PurchaseOrderForm, PurchaseOrderItemForm
)
from product.models import PaintingMaterialRequirement


# ============================================================
# Helper
# ============================================================

def _inventory_context(active_tab='dashboard'):
    return {
        'active_tab': active_tab,
    }


# ============================================================
# Dashboard
# ============================================================

@login_required
@admin_or_manager_required
def inventory_dashboard(request):
    total_materials = RawMaterial.objects.filter(is_active=True).count()
    total_suppliers = Supplier.objects.filter(is_active=True).count()
    low_stock = RawMaterial.objects.filter(is_active=True).annotate(
        stock=Sum(
            Case(
                When(movements__movement_type='consumption', then=-F('movements__quantity')),
                default=F('movements__quantity'),
                output_field=DecimalField()
            )
        )
    ).filter(stock__lte=F('min_stock_alert')).count()

    recent_movements = StockMovement.objects.select_related(
        'raw_material', 'supplier', 'created_by'
    ).order_by('-created_at')[:10]

    pending_orders = PurchaseOrder.objects.filter(status__in=['draft', 'ordered']).count()
    pending_issues = MaterialIssue.objects.filter(status='requested').count()

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

    if not task.order_item_id:
        return rows
    bom = task.order_item.product.bom.select_related('part__material__raw_material')
    if task.color_part:
        bom = bom.filter(color_part=task.color_part)
    for entry in bom:
        raw = entry.part.material.raw_material
        if raw:
            rows.append((raw, Decimal(task.quantity) * entry.quantity * entry.part.material.consumption_per_unit))
    return rows


@login_required
@admin_or_manager_required
def production_issue_queue(request):
    """A warehouse-first queue: one line is one material required by one production task."""
    from product.models import ProductionTask

    if request.method == 'POST' and request.POST.get('action') == 'request':
        task = get_object_or_404(ProductionTask, pk=request.POST.get('task_id'))
        raw = get_object_or_404(RawMaterial, pk=request.POST.get('raw_material_id'))
        quantity = Decimal(request.POST.get('quantity', '0'))
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

    station = request.GET.get('station', 'paint')
    tasks = ProductionTask.objects.filter(status__in=['pending', 'waiting']).select_related(
        'order', 'order_item__product', 'part__material__raw_material', 'painting_stage'
    ).order_by('scheduled_start', 'order_id')
    if station:
        tasks = tasks.filter(station_name=station)

    existing = {(issue.task_id, issue.raw_material_id) for issue in MaterialIssue.objects.exclude(status='cancelled')}
    requirements = []
    for task in tasks:
        for raw, quantity in _task_material_requirements(task):
            if (task.id, raw.id) not in existing:
                requirements.append({'task': task, 'raw_material': raw, 'quantity': quantity})

    issues = MaterialIssue.objects.filter(status__in=['requested', 'partial']).select_related(
        'raw_material', 'task__order', 'task__order_item__product', 'defect__order', 'defect__part'
    )
    return render(request, 'inventory/production_issue_queue.html', {
        **_inventory_context('production_queue'), 'requirements': requirements, 'issues': issues,
        'station': station, 'stations': ProductionTask.STATION_CHOICES,
    })


@login_required
@admin_or_manager_required
@require_http_methods(['POST'])
def issue_material(request, issue_id):
    """Confirm hand-over and create the inventory consumption record in the same transaction."""
    with atomic():
        issue = get_object_or_404(MaterialIssue.objects.select_for_update().select_related('raw_material'), pk=issue_id)
        quantity = Decimal(request.POST.get('quantity', str(issue.requested_quantity)))
        remaining = issue.requested_quantity - issue.issued_quantity
        if issue.status not in ['requested', 'partial']:
            messages.error(request, 'این درخواست قبلاً تعیین تکلیف شده است.')
        elif quantity <= 0 or quantity > remaining:
            messages.error(request, 'مقدار تحویل باید بین صفر و مانده درخواست باشد.')
        elif issue.raw_material.current_stock < quantity:
            messages.error(request, f'موجودی کافی نیست. موجودی فعلی: {issue.raw_material.current_stock}')
        else:
            StockMovement.objects.create(
                raw_material=issue.raw_material, movement_type='consumption', quantity=quantity,
                reference_task=issue.task, created_by=request.user,
                note=f'تحویل انبار #{issue.id} — {issue.get_purpose_display()}'
            )
            issue.issued_quantity += quantity
            issue.status = 'issued' if issue.issued_quantity >= issue.requested_quantity else 'partial'
            issue.issued_by = request.user
            issue.issued_at = timezone.now()
            issue.save(update_fields=['issued_quantity', 'status', 'issued_by', 'issued_at'])
            if issue.defect_id and issue.status == 'issued':
                issue.defect.status = 'rework_issued'
                issue.defect.save(update_fields=['status'])
            messages.success(request, 'تحویل مواد و خروج انبار ثبت شد.')
    return redirect('inventory:production_issue_queue')


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
        'category': material.category_id,
        'unit': material.unit,
        'min_stock_alert': str(material.min_stock_alert),
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
        po = PurchaseOrder.objects.select_for_update().get(pk=order_id)
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
        stock=Coalesce(Sum('movements__quantity'), Value(0, output_field=DecimalField()))
    ).filter(stock__lte=F('min_stock_alert')).order_by('stock')

    context = {
        **_inventory_context('low_stock'),
        'materials': materials,
    }
    return render(request, 'inventory/low_stock.html', context)
