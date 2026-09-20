import io
import ast
import json
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta
from xml.dom import minidom

import pandas as pd
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db import models, transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import (get_list_or_404, get_object_or_404, redirect,
                               render)

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from .models import Order, Customer, OrderItem, Color, Product, ProductCategory
from .forms import CustomerInfoForm, OrderItemForm, ColorSelectionForm
from django.urls import reverse
from django.utils import timezone
from django.forms import inlineformset_factory, modelformset_factory
from .decorators import admin_or_manager_required , staff_or_representative_required
from .models import *
from .forms import *
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Q, Count, F, OuterRef, Subquery, IntegerField, Exists
from django.shortcuts import render
from .decorators import staff_or_representative_required
from .models import OrderItem, ProductCategory, Product, STATION_CHOICES, PackagingUnit
import jdatetime
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone

from .decorators import admin_or_manager_required
from .models import Order, OrderItem, ProductionTask, PackagingUnit, STATION_CHOICES
from django.views.decorators.http import require_POST

import datetime as dt
import pandas as pd
import jdatetime
import math
from django.db import models

import traceback  # ← اضافه کنید
from decimal import Decimal

# تمام توابع مورد نیاز از utils را وارد کنید
from .utils import (
    parse_jalali_date,
    create_and_schedule_items_for_date,
    get_painting_ready_items_queryset,
    get_item_paint_preview,
    repaint_item_ids_for_date,
    _get_process_cache,
    assign_task_to_worker,
    get_unique_color_codes_for_item,
    log_production_event,
    auto_create_material_issues,
    _parse_default_colors,
    get_painting_process_for_color,
    get_color_hex_map,
    get_color_code_choices,
    auto_assign_paint_tasks,
)
logger = logging.getLogger(__name__)


@login_required
@admin_or_manager_required
def production_defects(request):
    from inventory.models import RawMaterial, MaterialIssue

    if request.method == 'POST':
        order = get_object_or_404(Order, pk=request.POST.get('order_id'))
        unit = get_object_or_404(
            PackagingUnit.objects.select_related('order_item__order', 'order_item__product').filter(
                order_item__order=order
            ),
            pk=request.POST.get('packaging_unit_id')
        )
        item = unit.order_item
        color_part = request.POST.get('color_part', '').strip()
        try:
            quantity = int(request.POST.get('quantity', 0) or 0)
        except (TypeError, ValueError):
            quantity = 0
        description = request.POST.get('description', '').strip()
        available_parts = list(item.ordercolor.values_list('part', flat=True))
        if not available_parts:
            available_parts = list(_parse_default_colors(item.product).keys())

        if quantity < 1 or not description or not color_part or color_part not in available_parts:
            messages.error(request, 'بخش رنگی، تعداد خراب و شرح خرابی الزامی است.')
        else:
            related_task = ProductionTask.objects.filter(
                order_item=item, station_name='paint', color_part=color_part
            ).order_by('-step_order').first()

            defect = ProductionDefect.objects.create(
                task=related_task, order=item.order, order_item=item,
                packaging_unit=unit, color_part=color_part, quantity=quantity,
                description=description, reported_by=request.user,
            )
            raw_id = request.POST.get('raw_material_id')
            replacement_quantity = request.POST.get('replacement_quantity')
            if raw_id and replacement_quantity:
                try:
                    replacement_quantity = Decimal(replacement_quantity)
                    if replacement_quantity > 0:
                        MaterialIssue.objects.create(
                            task=related_task, defect=defect, packaging_unit=unit, raw_material_id=raw_id,
                            requested_quantity=replacement_quantity, purpose='rework',
                            requested_by=request.user, note=f'ساخت مجدد برای خرابی #{defect.id}'
                        )
                        defect.status = 'material_requested'
                        defect.save(update_fields=['status'])
                except (ValueError, ArithmeticError):
                    messages.warning(request, 'خرابی ثبت شد، اما مقدار مواد جایگزین معتبر نبود.')
            messages.success(request, 'خرابی ثبت شد.')
            return redirect('product_defects')

    orders = Order.objects.filter(status__in=['planned', 'producing']).select_related('customer').order_by('-id')[:200]
    defects = ProductionDefect.objects.select_related(
        'order', 'order_item__product', 'packaging_unit__order_item__product',
        'reported_by', 'task__painting_stage__process'
    ).prefetch_related('material_issues__raw_material')

    return render(request, 'production_defects.html', {
        'orders': orders,
        'defects': defects,
        'raw_materials': RawMaterial.objects.filter(is_active=True).order_by('name'),
        'color_parts': Color.PART_CHOICES,
    })


@login_required
@admin_or_manager_required
def ajax_order_units_for_defect(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    units = PackagingUnit.objects.filter(
        order_item__order=order
    ).select_related('order_item__product__category').order_by(
        'order_item_id', 'unit_number'
    )
    data = []
    for unit in units:
        category_name = unit.order_item.product.category.name if unit.order_item.product.category else ''
        data.append({
            'id': unit.id,
            'text': f"{category_name} {unit.order_item.product.name} — واحد #{unit.unit_number} (بارکد #{unit.id})"
        })
    return JsonResponse({'results': data})


@login_required
@admin_or_manager_required
def ajax_unit_color_parts_for_defect(request, unit_id):
    unit = get_object_or_404(
        PackagingUnit.objects.select_related('order_item__product'), pk=unit_id
    )
    parts = list(dict.fromkeys(unit.order_item.ordercolor.values_list('part', flat=True)))
    if not parts:
        parts = list(_parse_default_colors(unit.order_item.product).keys())
    label_map = dict(Color.PART_CHOICES)
    data = [{'value': part, 'text': label_map.get(part, part)} for part in parts]
    return JsonResponse({'results': data})


@login_required
@admin_or_manager_required
def ajax_order_items_for_defect(request, order_id):
    """آیتم‌های یک سفارش برای dropdown دوم فرم ثبت خرابی."""
    order = get_object_or_404(Order, pk=order_id)
    items = order.items.select_related('product__category').all()
    data = [{'id': i.id, 'text': f"{i.product.category.name} {i.product.name} (×{i.quantity})"} for i in items]
    return JsonResponse({'results': data})


@login_required
@admin_or_manager_required
def ajax_item_color_parts_for_defect(request, item_id):
    """بخش‌های رنگی موجود روی یک آیتم (از ordercolor یا default_colors محصول)."""
    item = get_object_or_404(OrderItem, pk=item_id)
    parts = list(item.ordercolor.values_list('part', flat=True))
    if not parts:
        parts = list(_parse_default_colors(item.product).keys())
    label_map = dict(Color.PART_CHOICES)
    data = [{'value': p, 'text': label_map.get(p, p)} for p in parts]
    return JsonResponse({'results': data})


@login_required
@staff_or_representative_required
def report_fulfillment_status(request):
    """Representative-centric view of complete, partial and unsent packed items."""
    representative_id = request.GET.get('representative', '')
    items = OrderItem.objects.select_related('order__user', 'order__customer', 'product').annotate(
        total_units=Count('packaging_units', distinct=True),
        packed_units=Count('packaging_units', filter=Q(packaging_units__is_packed=True), distinct=True),
        shipped_units=Count('packaging_units', filter=Q(packaging_units__is_shipped=True), distinct=True),
    ).order_by('order__user__username', 'order_id', 'product__name')
    if representative_id:
        items = items.filter(order__user_id=representative_id)
    if not request.user.is_staff:
        items = items.filter(order__user=request.user)

    for item in items:
        if item.total_units and item.shipped_units == item.total_units:
            item.fulfillment_status, item.fulfillment_badge = 'ارسال کامل', 'success'
        elif item.packed_units == item.total_units and item.total_units:
            item.fulfillment_status, item.fulfillment_badge = 'بسته‌بندی کامل؛ آماده ارسال', 'primary'
        else:
            item.fulfillment_status, item.fulfillment_badge = 'ناقص در بسته‌بندی / ارسال', 'warning'
    representatives = User.objects.filter(order__items__isnull=False).distinct().order_by('username') if request.user.is_staff else []
    return render(request, 'reports/fulfillment_status.html', {
        'items': items, 'representatives': representatives, 'selected_representative': representative_id,
    })



# views.py - اضافه کنید

import json
from django.http import JsonResponse
from django.db.models import Q, Count
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.template.loader import render_to_string
from .decorators import admin_or_manager_required
from .models import WorkerProfile, Product, OrderItem, PaintingStage, STATION_CHOICES


# ============================================================
# API لیست کارگران (با فیلتر و صفحه‌بندی)
# ============================================================
@login_required
@admin_or_manager_required
def painting_workers_api(request):
    if request.method == 'GET':
        search = request.GET.get('search', '')
        status_filter = request.GET.get('status', '')
        skill_filter = request.GET.get('skill', '')
        page = int(request.GET.get('page', 1))
        per_page = 20

        workers = WorkerProfile.objects.filter(stage='paint') \
            .select_related('user') \
            .prefetch_related('excluded_products', 'excluded_items') \
            .annotate(active_tasks=Count('user__assigned_tasks',
                filter=Q(user__assigned_tasks__station_name='paint',
                        user__assigned_tasks__status__in=['pending', 'waiting'])
            ))

        if search:
            workers = workers.filter(
                Q(user__username__icontains=search) |
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search)
            )
        if status_filter == 'active':
            workers = workers.filter(is_available=True)
        elif status_filter == 'inactive':
            workers = workers.filter(is_available=False)
        if skill_filter:
            workers = workers.filter(skills__contains=[skill_filter])

        paginator = Paginator(workers, per_page)
        page_obj = paginator.get_page(page)

        table_html = render_to_string('painting_management/_worker_rows.html', {
            'workers': page_obj,
            'skill_choices': PaintingStage.SKILL_CHOICES,
        })
        pagination_html = render_to_string('painting_management/_pagination.html', {
            'workers': page_obj,
        })

        return JsonResponse({
            'success': True,
            'table_html': table_html,
            'pagination_html': pagination_html,
            'total': paginator.count,
            'page': page,
            'total_pages': paginator.num_pages,
        })

    elif request.method == 'POST':
        try:
            data = json.loads(request.body)
            user_id = data.get('user_id')
            if not user_id:
                return JsonResponse({'success': False, 'error': 'کاربر انتخاب نشده است'})
            worker = WorkerProfile.objects.create(
                user_id=user_id,
                stage=data.get('stage', 'paint'),
                is_available=data.get('is_available', True),
                skills=data.get('skills', []),
                    skill_priority=data.get('skill_priority', data.get('skill_costs', {}))
            )
            return JsonResponse({'success': True, 'worker': worker_to_dict(worker)})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

    return JsonResponse({'success': False, 'error': 'روش غیرمجاز'})


# ============================================================
# API جزئیات یک کارگر
# ============================================================
@login_required
@admin_or_manager_required
@require_http_methods(['GET', 'PUT', 'DELETE'])
def painting_worker_detail_api(request, worker_id):
    worker = get_object_or_404(WorkerProfile, pk=worker_id, stage='paint')

    if request.method == 'GET':
        return JsonResponse({
            'success': True,
            'id': worker.id,
            'user_id': worker.user_id,
            'username': worker.user.username,
            'stage': worker.stage,
            'stage_label': dict(STATION_CHOICES).get(worker.stage, worker.stage),
            'skills': worker.skills,
            'skill_priority': worker.skill_priority,
            'is_available': worker.is_available,
            'excluded_products_count': worker.excluded_products.count(),
            'excluded_items_count': worker.excluded_items.count(),
        })

    elif request.method == 'PUT':
        try:
            data = json.loads(request.body)
            worker.user_id = data.get('user_id', worker.user_id)
            worker.stage = data.get('stage', worker.stage)
            worker.is_available = data.get('is_available', worker.is_available)
            worker.skills = data.get('skills', worker.skills)
            worker.skill_priority = data.get('skill_priority', data.get('skill_costs', worker.skill_priority))
            worker.save()
            return JsonResponse({'success': True, 'worker': worker_to_dict(worker)})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

    elif request.method == 'DELETE':
        worker.delete()
        return JsonResponse({'success': True})


# ============================================================
# API مدیریت ممنوعیت‌ها (محصولات/آیتم‌ها)
# ============================================================
@login_required
@admin_or_manager_required
def painting_worker_exclusion_api(request, worker_id):
    worker = get_object_or_404(WorkerProfile, pk=worker_id, stage='paint')

    if request.method == 'GET':
        items = []
        if 'products' in request.path:
            items = [{'id': p.id, 'text': p.name} for p in worker.excluded_products.all()]
        else:
            items = [{'id': i.id, 'text': f"{i.order.id} - {i.product.name}"} for i in worker.excluded_items.all()]
        return JsonResponse({'success': True, 'items': items})

    elif request.method == 'PUT':
        try:
            data = json.loads(request.body)
            item_ids = data.get('items', [])
            valid_ids = [int(i) for i in item_ids if str(i).isdigit()]
            if 'products' in request.path:
                worker.excluded_products.set(valid_ids)
            else:
                worker.excluded_items.set(valid_ids)
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

    return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)


# ============================================================
# API جستجوی محصولات و آیتم‌ها (برای Select2)
# ============================================================
@login_required
@admin_or_manager_required
def search_products_api(request):
    q = request.GET.get('q', '').strip()
    products = Product.objects.select_related('category')
    if q:
        products = products.filter(
            Q(name__icontains=q) | Q(category__name__icontains=q)
        )[:20]
    else:
        products = products.all()[:20]
    results = [{
        'id': p.id,
        'text': f"{p.category.name} - {p.name}"  # ← ترکیب دسته‌بندی و نام
    } for p in products]
    return JsonResponse({'results': results})


@login_required
@admin_or_manager_required
def search_items_api(request):
    q = request.GET.get('q', '')
    items = OrderItem.objects.select_related('order', 'product').filter(
        Q(order__id__icontains=q) | Q(product__name__icontains=q)
    )[:20]
    results = [{'id': i.id, 'text': f"#{i.order.id} - {i.product.name}"} for i in items]
    return JsonResponse({'results': results})


# ============================================================
# تابع کمکی
# ============================================================
def worker_to_dict(worker):
    return {
        'id': worker.id,
        'username': worker.user.username,
        'stage_label': dict(STATION_CHOICES).get(worker.stage, worker.stage),
        'skills': worker.skills,
        'skill_priority': worker.skill_priority,
        'is_available': worker.is_available,
        'excluded_products_count': worker.excluded_products.count(),
        'excluded_items_count': worker.excluded_items.count(),
        'active_tasks': worker.user.assigned_tasks.filter(
            station_name='paint', status__in=['pending', 'waiting']
        ).count()
    }



# views.py (اضافه کنید)

@login_required
@admin_or_manager_required
def admin_edit_order_item(request, item_id):
    """
    ویرایش یک آیتم سفارش توسط ادمین/مدیران (بدون محدودیت)
    """
    item = get_object_or_404(OrderItem, pk=item_id)
    order = item.order
    
    # رنگ‌های فعلی
    existing_colors = {c.part: c.code for c in item.ordercolor.all()}
    
    if request.method == 'POST':
        item_form = EditOrderItemForm(request.POST, instance=item)
        color_form = ColorSelectionForm(request.POST)
        
        if item_form.is_valid() and color_form.is_valid():
            with transaction.atomic():
                updated_item = item_form.save(commit=False)
                # اگر محصول تغییر کرده، قیمت را به‌روز کن
                if 'product' in item_form.changed_data:
                    updated_item.unit_price = updated_item.product.base_price
                updated_item.save()
                
                # حذف رنگ‌های قبلی و ایجاد جدید
                item.ordercolor.all().delete()
                for part_value, _ in Color.PART_CHOICES:
                    code = color_form.cleaned_data.get(f'color_{part_value}')
                    if code:
                        Color.objects.create(part=part_value, code=code, orderitem=item)
                
                messages.success(request, '✅ آیتم با موفقیت ویرایش شد.')
                return redirect('admin_edit_order', order_id=order.id)
        else:
            messages.error(request, '⚠️ خطا در ویرایش آیتم.')
    else:
        item_form = EditOrderItemForm(
            instance=item,
            initial={
                'category': item.product.category.id,
                'product': item.product.id,
            }
        )
        item_form.fields['product'].widget.attrs['data-initial-product'] = item.product.id
        color_form = ColorSelectionForm(initial={
            f'color_{part}': existing_colors.get(part, '')
            for part, _ in Color.PART_CHOICES
        })
    
    context = {
        'item_form': item_form,
        'color_form': color_form,
        'item': item,
        'order': order,
    }
    return render(request, 'admin_edit_order_item.html', context)


@login_required
@admin_or_manager_required
def admin_edit_order(request, order_id):
    """ویرایش سفارش توسط ادمین/مدیران"""
    order = get_object_or_404(Order, pk=order_id)
    
    if request.method == 'POST':
        # پردازش فرم اصلی سفارش
        form = OrderEditForm(request.POST, instance=order)
        if form.is_valid():
            form.save()
            messages.success(request, '✅ اطلاعات سفارش به‌روز شد.')
            return redirect('admin_edit_order', order_id=order.id)
        else:
            messages.error(request, '⚠️ خطا در ویرایش اطلاعات سفارش.')
    else:
        form = OrderEditForm(instance=order)

    # لیست آیتم‌ها
    items = order.items.select_related('product__category').prefetch_related('ordercolor')

    # فرم‌های افزودن آیتم جدید (همانند create_step2)
    item_form = OrderItemForm()
    color_form = ColorSelectionForm()

    context = {
        'order': order,
        'form': form,
        'items': items,
        'item_form': item_form,
        'color_form': color_form,
        'station_choices': STATION_CHOICES,
        'has_any_tasks': order.tasks.exists(),
        'has_paint_tasks': order.tasks.filter(station_name='paint').exists(),
    }
    return render(request, 'admin_order_edit.html', context)


@login_required
@admin_or_manager_required
def admin_delete_order_item(request, item_id):
    """حذف آیتم از سفارش (فقط ادمین)"""
    item = get_object_or_404(OrderItem, pk=item_id)
    order_id = item.order.id
    if request.method == 'POST':
        item.delete()
        messages.success(request, f'✅ آیتم حذف شد.')
    else:
        messages.warning(request, '⚠️ درخواست غیرمجاز.')
    return redirect('admin_edit_order', order_id=order_id)


@login_required
@admin_or_manager_required
def admin_add_order_item(request, order_id):
    """افزودن آیتم به سفارش (ادمین) - مشابه create_order_step2 اما با redirect به edit"""
    order = get_object_or_404(Order, pk=order_id)
    if request.method == 'POST':
        item_form = OrderItemForm(request.POST)
        color_form = ColorSelectionForm(request.POST)
        if item_form.is_valid() and color_form.is_valid():
            with transaction.atomic():
                product = item_form.cleaned_data['product']
                order_item = item_form.save(commit=False)
                order_item.order = order
                order_item.product = product
                order_item.unit_price = product.base_price
                order_item.save()

                for part_value, _ in Color.PART_CHOICES:
                    code = color_form.cleaned_data.get(f'color_{part_value}')
                    if code:
                        Color.objects.create(part=part_value, code=code, orderitem=order_item)
                messages.success(request, f'✅ آیتم "{product.name}" اضافه شد.')
                return redirect('admin_edit_order', order_id=order.id)
        else:
            messages.error(request, '⚠️ خطا در افزودن آیتم.')
    return redirect('admin_edit_order', order_id=order_id)


@login_required
@admin_or_manager_required
def admin_delete_order(request, order_id):
    """حذف کامل سفارش (فقط ادمین) - با تأیید"""
    order = get_object_or_404(Order, pk=order_id)
    if request.method == 'POST':
        order.delete()
        messages.success(request, f'✅ سفارش {order_id} با موفقیت حذف شد.')
        return redirect('order_list')
    else:
        messages.warning(request, '⚠️ درخواست غیرمجاز.')
        return redirect('admin_edit_order', order_id=order_id)





@login_required
@admin_or_manager_required
def admin_order_tasks(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    tasks = order.tasks.select_related('part', 'assigned_worker', 'painting_stage').order_by('step_order')
    
    if request.method == 'POST':
        task_id = request.POST.get('task_id')
        new_status = request.POST.get('status')
        if task_id and new_status in ['waiting', 'pending', 'done']:
            task = get_object_or_404(ProductionTask, pk=task_id, order=order)
            task.status = new_status
            task.save()  # save() به‌طور خودکار next_step را فعال می‌کند
            messages.success(request, f'وضعیت تسک {task.id} به {task.get_status_display()} تغییر یافت.')
        return redirect('admin_order_tasks', order_id=order.id)
    
    context = {
        'order': order,
        'tasks': tasks,
    }
    return render(request, 'admin_order_tasks.html', context)


@login_required
@admin_or_manager_required
def admin_delete_task(request, task_id):
    task = get_object_or_404(ProductionTask, pk=task_id)
    order_id = task.order.id
    if request.method == 'POST':
        task.delete()
        messages.success(request, 'تسک با موفقیت حذف شد.')
    return redirect('admin_order_tasks', order_id=order_id)


@login_required
@staff_or_representative_required
def scan_item_tasks_ajax(request, item_id):
    if not hasattr(request.user, 'workerprofile'):
        return JsonResponse({'success': False, 'error': 'پروفایل کاری ندارید'}, status=403)

    worker_stage = request.user.workerprofile.stage
    item = get_object_or_404(OrderItem.objects.select_related('order', 'product'), pk=item_id)

    from .utils import get_item_task_progress_for_station
    item_tasks = get_item_task_progress_for_station(item_id, worker_stage)

    return JsonResponse({
        'success': True,
        'item_tasks': item_tasks,
        'order_id': item.order_id,
        'item_id': item.id,
        'product_name': item.product.name if item.product else '',
    })


@login_required
@require_POST
def mark_task_done(request, task_id):
    task = get_object_or_404(ProductionTask, pk=task_id)

    with transaction.atomic():
        task.completed_quantity += 1
        if task.completed_quantity > task.quantity:
            task.completed_quantity = task.quantity
        if task.completed_quantity >= task.quantity:
            task.status = 'done'
            task.scanned_by = request.user
        task.save()

    return JsonResponse({
        'success': True,
        'completed_quantity': task.completed_quantity,
        'quantity': task.quantity,
        'status': task.status,
    })


@login_required
@admin_or_manager_required
def archive_upload(request):
    archive_type = request.GET.get('type', 'cnc').lower()
    if archive_type not in ('cnc', 'dr'):
        archive_type = 'cnc'

    if archive_type == 'cnc':
        source_dir = getattr(settings, 'CNC_SOURCE_DIR', '')
        expected_ext = getattr(settings, 'CNC_FILE_EXTENSION', '.cnc')
    else:
        source_dir = getattr(settings, 'DR_SOURCE_DIR', '')
        expected_ext = getattr(settings, 'DR_FILE_EXTENSION', '.scx')

    source_dir = str(source_dir)
    os.makedirs(source_dir, exist_ok=True)

    if request.method == 'POST':
        uploaded_file = request.FILES.get('archive_file')
        if not uploaded_file:
            messages.error(request, 'فایلی انتخاب نشده است.')
            return redirect('archive_upload')

        ext = os.path.splitext(uploaded_file.name)[1].lower()
        if ext != expected_ext.lower():
            messages.error(request, f'فایل باید با پسوند {expected_ext} باشد.')
            return redirect('archive_upload')

        filename = os.path.basename(uploaded_file.name)
        filename = re.sub(r'[\\/*?:"<>|]', '', filename)
        file_path = os.path.join(source_dir, filename)

        with open(file_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)

        messages.success(request, f'فایل {filename} با موفقیت آپلود شد.')
        return redirect(f"{reverse('archive_upload')}?type={archive_type}")

    search_query = request.GET.get('q', '').strip().lower()
    files = []
    try:
        for fname in os.listdir(source_dir):
            if search_query and search_query not in fname.lower():
                continue
            fpath = os.path.join(source_dir, fname)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                files.append({
                    'name': fname,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                })
    except OSError:
        pass

    files.sort(key=lambda x: x['name'])

    context = {
        'archive_type': archive_type,
        'files': files,
        'search_query': search_query,
        'expected_ext': expected_ext,
    }
    return render(request, 'archive_upload.html', context)


def _archive_upload_page(request, archive_type):
    if archive_type == 'cnc':
        source_dir = getattr(settings, 'CNC_SOURCE_DIR', '')
        expected_ext = getattr(settings, 'CNC_FILE_EXTENSION', '.cnc')
    else:
        source_dir = getattr(settings, 'DR_SOURCE_DIR', '')
        expected_ext = getattr(settings, 'DR_FILE_EXTENSION', '.scx')

    source_dir = str(source_dir)
    os.makedirs(source_dir, exist_ok=True)

    if request.method == 'POST':
        uploaded_file = request.FILES.get('archive_file')
        if not uploaded_file:
            messages.error(request, 'فایلی انتخاب نشده است.')
            return redirect(f"{reverse('archive_upload')}?type={archive_type}")

        ext = os.path.splitext(uploaded_file.name)[1].lower()
        if ext != expected_ext.lower():
            messages.error(request, f'فایل باید با پسوند {expected_ext} باشد.')
            return redirect(f"{reverse('archive_upload')}?type={archive_type}")

        filename = os.path.basename(uploaded_file.name)
        filename = re.sub(r'[\\/*?:"<>|]', '', filename)
        file_path = os.path.join(source_dir, filename)

        with open(file_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)

        messages.success(request, f'فایل {filename} با موفقیت آپلود شد.')
        return redirect(f"{reverse('archive_upload')}?type={archive_type}")

    search_query = request.GET.get('q', '').strip().lower()
    files = []
    try:
        for fname in os.listdir(source_dir):
            if search_query and search_query not in fname.lower():
                continue
            fpath = os.path.join(source_dir, fname)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                files.append({
                    'name': fname,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                })
    except OSError:
        pass

    files.sort(key=lambda x: x['name'])

    context = {
        'archive_type': archive_type,
        'files': files,
        'search_query': search_query,
        'expected_ext': expected_ext,
    }
    return render(request, f'archive_upload_{archive_type}.html', context)


@login_required
@admin_or_manager_required
def archive_upload_cnc(request):
    return _archive_upload_page(request, 'cnc')


@login_required
@admin_or_manager_required
def archive_upload_dr(request):
    return _archive_upload_page(request, 'dr')


@login_required
@admin_or_manager_required
def archive_delete_file(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'روش غیرمجاز'}, status=405)

    filename = ''
    archive_type = 'cnc'
    if request.content_type == 'application/json':
        try:
            import json
            data = json.loads(request.body.decode('utf-8'))
            filename = (data.get('filename') or '').strip()
            archive_type = (data.get('archive_type') or 'cnc').lower()
        except Exception:
            pass
    else:
        filename = request.POST.get('filename', '').strip()
        archive_type = request.POST.get('archive_type', 'cnc').lower()

    if not filename:
        return JsonResponse({'success': False, 'error': 'نام فایل مشخص نشده است.'}, status=400)

    if archive_type == 'cnc':
        source_dir = str(getattr(settings, 'CNC_SOURCE_DIR', ''))
    else:
        source_dir = str(getattr(settings, 'DR_SOURCE_DIR', ''))

    file_path = os.path.join(source_dir, os.path.basename(filename))
    if not os.path.exists(file_path):
        return JsonResponse({'success': False, 'error': 'فایل یافت نشد.'}, status=404)

    os.remove(file_path)
    return JsonResponse({'success': True})


@login_required
@admin_or_manager_required
def archive_download(request, archive_type, filename):
    archive_type = archive_type.lower()
    if archive_type == 'cnc':
        source_dir = str(getattr(settings, 'CNC_SOURCE_DIR', ''))
    else:
        source_dir = str(getattr(settings, 'DR_SOURCE_DIR', ''))

    file_path = os.path.join(source_dir, os.path.basename(filename))
    if not os.path.exists(file_path):
        raise Http404('فایل یافت نشد.')

    with open(file_path, 'rb') as f:
        response = HttpResponse(f.read(), content_type='application/octet-stream')
        response['Content-Disposition'] = f'attachment; filename="{os.path.basename(filename)}"'
        return response


@login_required
@admin_or_manager_required
def admin_tasks_management(request):
    tasks = ProductionTask.objects.select_related(
        'order', 'part', 'order_item', 'assigned_worker', 'painting_stage'
    ).order_by('order__id', 'step_order')

    search_query = request.GET.get('q', '')
    station_filter = request.GET.get('station', '')
    status_filter = request.GET.get('status', '')
    unit_filter = request.GET.get('unit', '')
    order_filter = request.GET.get('order', '')

    if search_query:
        tasks = tasks.filter(
            Q(order__number__icontains=search_query) |
            Q(order__customer__name__icontains=search_query) |
            Q(part__name__icontains=search_query) |
            Q(order_item__product__name__icontains=search_query)
        )
    if station_filter:
        tasks = tasks.filter(station_name=station_filter)
    if status_filter:
        tasks = tasks.filter(status=status_filter)
    if order_filter:
        tasks = tasks.filter(order_id=order_filter)
    if unit_filter:
        tasks = tasks.filter(order_item_id=unit_filter)

    unit_qs = tasks.filter(order_item__isnull=False).values(
        'order_id', 'order_item_id'
    ).distinct().order_by('order_id', 'order_item_id')

    if request.method == 'POST':
        action = request.POST.get('action')
        task_ids = request.POST.getlist('task_ids')

        if action == 'fix_chain':
            if task_ids:
                orders = Order.objects.filter(tasks__id__in=task_ids).distinct()
            else:
                orders = Order.objects.filter(tasks__in=tasks).distinct()
            fixed = 0
            for order in orders:
                order_tasks = order.tasks.order_by('step_order')
                for task in order_tasks:
                    if task.status == 'done':
                        if task.station_name == 'paint' and task.order_item_id:
                            next_task = order.tasks.filter(
                                station_name='paint',
                                order_item=task.order_item,
                                color_part=task.color_part,
                                step_order=task.step_order + 1,
                            ).first()
                        else:
                            next_task = order.tasks.filter(
                                part=task.part,
                                step_order=task.step_order + 1,
                            ).first()
                        if next_task and next_task.status == 'waiting':
                            next_task.status = 'pending'
                            next_task.save(update_fields=['status'])
                            fixed += 1
            messages.success(request, f'تعداد {fixed} وظیفه بعدی فعال شد.')
        elif action == 'bulk_status':
            new_status = request.POST.get('bulk_status')
            qs = ProductionTask.objects.filter(id__in=task_ids)
            if new_status in dict(ProductionTask.TASK_STATUS):
                count = qs.count()
                for task in qs:
                    old_status = task.status
                    task.status = new_status
                    if new_status == 'done' and old_status != 'done':
                        task.completed_at = jdatetime.date.today()
                        task.completed_quantity = task.quantity
                        log_production_event(
                            task=task,
                            event_type='status_changed',
                            user=request.user,
                            old_status=old_status,
                            new_status=new_status,
                        )
                    task.save(update_fields=['status', 'completed_at', 'completed_quantity'])
                messages.success(request, f'وضعیت {count} وظیفه به «{dict(ProductionTask.TASK_STATUS)[new_status]}» تغییر یافت.')
        elif action == 'bulk_worker':
            worker_id = request.POST.get('bulk_worker')
            if worker_id:
                count = ProductionTask.objects.filter(id__in=task_ids).count()
                ProductionTask.objects.filter(id__in=task_ids).update(assigned_worker_id=worker_id)
                messages.success(request, f'کارگر برای {count} وظیفه تخصیص داده شد.')
        elif action == 'bulk_delete':
            count = ProductionTask.objects.filter(id__in=task_ids).count()
            ProductionTask.objects.filter(id__in=task_ids).delete()
            messages.success(request, f'تعداد {count} وظیفه حذف شد.')

        return redirect('admin_tasks_management')

    orders = Order.objects.all().order_by('-id')[:100]
    users = User.objects.filter(is_superuser=False).order_by('username')

    context = {
        'tasks': tasks,
        'station_choices': STATION_CHOICES,
        'task_status_choices': ProductionTask.TASK_STATUS,
        'search_query': search_query,
        'station_filter': station_filter,
        'status_filter': status_filter,
        'unit_filter': unit_filter,
        'order_filter': order_filter,
        'orders': orders,
        'units': unit_qs,
        'users': users,
    }
    return render(request, 'admin_tasks_management.html', context)
















@login_required
@staff_or_representative_required
@require_POST
def order_generate_tasks(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    result = order.generate_tasks()
    if result.get('success'):
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'error': result.get('error', 'تسک‌ها قبلاً ایجاد شده‌اند.')})




@login_required
@admin_or_manager_required
def dashboard(request):
    # آمار کلی سفارشات
    total_orders = Order.objects.count()
    active_orders = Order.objects.filter(status__in=['draft', 'planned', 'producing']).count()
    completed_orders = Order.objects.filter(status='completed').count()
    producing_orders = Order.objects.filter(status='producing').count()

    # ارسال‌های امروز
    today_shamsi = jdatetime.date.today()
    today_gregorian = today_shamsi.togregorian()
    shipped_today = PackagingUnit.objects.filter(
        is_shipped=True,
        shipped_at__date=today_gregorian
    ).count()

    # بار کاری ایستگاه‌ها
    station_load = []
    for code, name in STATION_CHOICES:
        pending = ProductionTask.objects.filter(station_name=code, status='pending').count()
        waiting = ProductionTask.objects.filter(station_name=code, status='waiting').count()
        station_load.append({
            'name': name,
            'pending': pending,
            'waiting': waiting,
            'total': pending + waiting,
        })

    # آخرین سفارشات
    latest_orders = Order.objects.select_related('customer', 'user').order_by('-id')[:5]

    context = {
        'total_orders': total_orders,
        'active_orders': active_orders,
        'completed_orders': completed_orders,
        'producing_orders': producing_orders,
        'shipped_today': shipped_today,
        'station_load': station_load,
        'latest_orders': latest_orders,
        'today_shamsi': today_shamsi,
    }
    return render(request, 'dashboard.html', context)














# -------------------------------------------------------------------
#     سفارش ها و پرینت فرم ها
# -------------------------------------------------------------------

# @login_required
# @staff_or_representative_required
# def order_list(request):
#     orders = Order.objects.select_related('customer').all()

#     # فیلتر وضعیت (دکمه‌ها)
#     status = request.GET.get('status')
#     if status:
#         orders = orders.filter(status=status)

#     # جستجوی عمومی (شماره یا نام مشتری)
#     q = request.GET.get('q')
#     if q:
#         orders = orders.filter(
#             models.Q(id__icontains=q) |
#             models.Q(customer__name__icontains=q) 
#             # models.Q(customerr__icontains=q)
#         )

#     # فیلترهای اختصاصی ستون‌ها
#     id_filter = request.GET.get('id_filter')
#     if id_filter:
#         orders = orders.filter(id__icontains=id_filter)

#     customer_filter = request.GET.get('customer_filter')
#     if customer_filter:
#         orders = orders.filter(customer__name__icontains=customer_filter)

#     date_from = request.GET.get('date_from')
#     if date_from:
#         orders = orders.filter(created_at__gte=date_from)

#     date_to = request.GET.get('date_to')
#     if date_to:
#         orders = orders.filter(created_at__lte=date_to)

#     # مرتب‌سازی
#     sort = request.GET.get('sort', '-id')
#     orders = orders.order_by(sort)

#     # صفحه‌بندی
#     paginator = Paginator(orders, 200)
#     page_number = request.GET.get('page')
#     page_obj = paginator.get_page(page_number)

#     context = {
#         'orders': page_obj,
#         'status_filter': status,
#         'search_query': q,
#         'id_filter': id_filter,
#         'customer_filter': customer_filter,
#         'date_from': date_from,
#         'date_to': date_to,
#         'sort': sort,
#     }
#     return render(request, 'order_list.html', context)



@login_required
@staff_or_representative_required
def order_list(request):
    orders = Order.objects.select_related('customer', 'user').prefetch_related('items__packaging_units').all().order_by('-id')

    status = request.GET.get('status')
    if status:
        orders = orders.filter(status=status)

    q = request.GET.get('q')
    if q:
        orders = orders.filter(
            Q(id__icontains=q) |
            Q(number__icontains=q) |
            Q(customer__name__icontains=q) |
            Q(user__username__icontains=q) |
            Q(user__first_name__icontains=q) |
            Q(user__last_name__icontains=q) |
            Q(items__id__icontains=q) |
            Q(items__product__name__icontains=q) |
            Q(items__product__category__name__icontains=q)
        ).distinct()

    id_filter = request.GET.get('id_filter')
    if id_filter:
        orders = orders.filter(id__icontains=id_filter)

    customer_filter = request.GET.get('customer_filter')
    if customer_filter:
        orders = orders.filter(customer__name__icontains=customer_filter)

    orders = orders.annotate(
        total_pack=Count('items__packaging_units', distinct=True),
        packed_count=Count('items__packaging_units', filter=Q(items__packaging_units__is_packed=True), distinct=True),
        shipped_count=Count('items__packaging_units', filter=Q(items__packaging_units__is_shipped=True), distinct=True),
    )

    packaging_status = request.GET.get('packaging_status')
    if packaging_status == 'done':
        orders = orders.filter(total_pack__gt=0, packed_count=F('total_pack'))
    elif packaging_status == 'pending':
        orders = orders.filter(total_pack__gt=0, packed_count__lt=F('total_pack'))
    elif packaging_status == 'none':
        orders = orders.filter(total_pack=0)

    shipping_status = request.GET.get('shipping_status')
    if shipping_status == 'done':
        orders = orders.filter(total_pack__gt=0, shipped_count=F('total_pack'))
    elif shipping_status == 'pending':
        orders = orders.filter(total_pack__gt=0, shipped_count__lt=F('total_pack'))
    elif shipping_status == 'none':
        orders = orders.filter(total_pack=0)

    paginator = Paginator(orders, 200)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'orders': page_obj,
        'status_filter': status,
        'search_query': q,
        'id_filter': id_filter,
        'customer_filter': customer_filter,
        'packaging_filter': packaging_status or '',
        'shipping_filter': shipping_status or '',
    }
    return render(request, 'order_list.html', context)


@login_required
@staff_or_representative_required
def order_items_expand_ajax(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related(
            'items__product__category',
            'items__logs',
            'items__packaging_units',
        ),
        pk=order_id
    )

    production_stations = [c for c, _ in STATION_CHOICES if c not in ('packaging', 'shipping')]
    station_label = dict(STATION_CHOICES)

    rows = []
    for item in order.items.all():
        done_stages = {log.stage for log in item.logs.all() if log.stage in production_stations}
        last_stage_code = None
        for code in production_stations:
            if code in done_stages:
                last_stage_code = code
        last_stage = station_label.get(last_stage_code) if last_stage_code else 'شروع نشده'

        packed, total = item.packaging_progress
        shipped, _ = item.shipping_progress

        rows.append({
            'item': item,
            'last_stage': last_stage,
            'packed': packed,
            'shipped': shipped,
            'total_units': total,
        })

    html = render_to_string('_order_items_expand.html', {'items': rows}, request=request)
    return JsonResponse({'success': True, 'html': html})









@login_required
@staff_or_representative_required
def order_item_list(request):
    items = OrderItem.objects.select_related(
        'order__user', 'product__category'
    ).prefetch_related(
        'logs', 'ordercolor'
    ).order_by('-order__created_at')

    # جستجوی عمومی
    q = request.GET.get('q')
    if q:
        items = items.filter(
            Q(order__id__icontains=q) |
            Q(id__icontains=q) |
            Q(order__customer__user__username__icontains=q) |
            Q(product__name__icontains=q) |
            Q(product__category__name__icontains=q)
        )

    # فیلترهای کشویی
    customer_id = request.GET.get('customer')
    if customer_id:
        items = items.filter(order__customer__user_id=customer_id)

    category_id = request.GET.get('category')
    if category_id:
        items = items.filter(product__category_id=category_id)

    product_id = request.GET.get('product')
    if product_id:
        items = items.filter(product_id=product_id)

    # مرتب‌سازی
    sort = request.GET.get('sort', '-id')
    items = items.order_by(sort)

    # صفحه‌بندی
    paginator = Paginator(items, 200)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # وضعیت مراحل برای هر آیتم (تبدیل به تاریخ شمسی)
    for item in page_obj:
        stage_dates = {}
        for log in item.logs.all():
            if log.created_at:
                # log.created_at یک jdatetime.date است
                jdate = log.created_at
                stage_dates[log.stage] = f"{jdate.month:02d}/{jdate.day:02d}"
        item.stage_dates = stage_dates
        item.color = item.color_summary

    # لیست‌های پایه برای فیلترهای کشویی
    customers = User.objects.all()
    categories = ProductCategory.objects.all()

    # محصولات: اگر دسته‌ای انتخاب شده باشد، فقط محصولات آن دسته را بفرستیم
    if category_id:
        products = Product.objects.filter(category_id=category_id).order_by('name')
    else:
        products = Product.objects.none()

    context = {
        'items': page_obj,
        'station_choices': STATION_CHOICES,
        'search_query': q,
        'customers': customers,
        'categories': categories,
        'products': products,
        'selected_customer': customer_id,
        'selected_category': category_id,
        'selected_product': product_id,
        'sort': sort,
    }
    return render(request, 'order_item.html', context)


@login_required
@staff_or_representative_required
def item_detail(request, pk):
    item = get_object_or_404(
        OrderItem.objects.select_related('product', 'order__customer', 'order__user', 'product__category').prefetch_related('logs'),
        pk=pk
    )
    
    # ۱. وضعیت کلی ایستگاه‌ها (از روی ProductionLog)
    stage_status_list = []
    for code, name in STATION_CHOICES:
        log = item.logs.filter(stage=code).first()
        if log and log.created_at:
            jdate = log.created_at
            stage_status_list.append(f"{jdate.month:02d}/{jdate.day:02d}")
        else:
            stage_status_list.append(None)
    
    # ۲. دریافت همه تسک‌های این سفارش
    all_tasks = ProductionTask.objects.filter(order=item.order).select_related('part')
    
    # ۳. ساخت نگاشت از part_id به station -> status
    # همچنین نگاشت از base_part_id برای قطعات داینامیک
    task_map = {}
    for task in all_tasks:
        if not task.part:
            continue
        task_map[(task.part_id, task.station_name)] = task.status
        if task.part.base_part_id:
            # اگر قطعه داینامیک است، وضعیت را به قطعه اصلی هم نسبت بده
            task_map[(task.part.base_part_id, task.station_name)] = task.status
    
    # ۴. ساخت لیست قطعات BOM
    bom_parts = []
    for bom_entry in item.product.bom.select_related('part').all():
        part = bom_entry.part
        station_status = {}
        for code, name in STATION_CHOICES:
            status = task_map.get((part.id, code))
            station_status[code] = status
        bom_parts.append({
            'part': part,
            'quantity': bom_entry.quantity,
            'station_status': station_status,
        })
    
    # اطلاعات اضافه: تاریخ سفارش، دسته‌بندی، نماینده
    order_date = item.order.created_at
    if hasattr(order_date, 'strftime'):
        order_date_str = order_date.strftime('%Y/%m/%d')
    else:
        order_date_str = str(order_date)
    
    category_name = item.product.category.name if item.product.category else '—'
    representative_name = item.order.user.get_full_name() or item.order.user.username if item.order.user else '—'
    
    context = {
        'item': item,
        'stage_status_list': stage_status_list,
        'station_choices': STATION_CHOICES,
        'bom_parts': bom_parts,
        'has_paint_tasks': item.paint_tasks.exists(),
        'order_date_str': order_date_str,
        'category_name': category_name,
        'representative_name': representative_name,
    }
    return render(request, 'item.html', context)


@login_required
@staff_or_representative_required
def scan_qr(request, pk):
    item = get_object_or_404(OrderItem.objects.select_related('order'), pk=pk)

    if not hasattr(request.user, 'workerprofile'):
        messages.error(request, "پروفایل کاری ندارید")
        return redirect('item_detail', pk=pk)

    stage = request.user.workerprofile.stage

    # جلوگیری از ثبت تکراری
    if ProductionLog.objects.filter(order_item=item, stage=stage).exists():
        messages.warning(request, "قبلاً ثبت شده")
        return redirect('item_detail', pk=pk)

    # ثبت لاگ
    ProductionLog.objects.create(
        order_item=item,
        stage=stage,
        user=request.user
    )

    # آپدیت تسک واقعی (اولین تسک pending مرتبط با این سفارش و ایستگاه)
    task = ProductionTask.objects.filter(
        order=item.order,
        station_name=stage,
        status='pending'
    ).first()

    if not task:
        messages.error(request, "مرحله مجاز نیست")
        return redirect('item_detail', pk=pk)

    task.status = 'done'
    task.scanned_by = request.user
    task.save()

    messages.success(request, "مرحله ثبت شد ✅")
    return redirect('item_detail', pk=pk)


@login_required
@admin_or_manager_required
def print_sheet(request, pk):
    item = get_object_or_404(
        OrderItem.objects.select_related('order__customer', 'product__category'),
        pk=pk
    )
    item.color = item.color_summary
    bom_list = item.product.bom.select_related('part').all()
    item.bompart = [{'part': b.part, 'quantity': b.quantity} for b in bom_list]
    return render(request, 'print.html', {'item': item})


@login_required
@admin_or_manager_required
def print_lable(request, pk):
    item = get_object_or_404(OrderItem.objects.select_related('order__customer', 'product__category'),pk=pk)
    item.color = item.color_summary
    return render(request, 'print_lable.html', {'item': item})

@login_required
@admin_or_manager_required
def order_print(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related('items__product__category', 'items__ordercolor'),
        id=order_id
    )
    items_data = []
    for item in order.items.all():
        items_data.append({
            'id': item.id,
            'product': item.product.name,
            'category': item.product.category.name,
            'quantity': item.quantity,
            'size': item.size,
            'colors': item.color_summary,
            'notes': item.notes
        })
    return render(request, 'order_print.html', {
        'order': order,
        'items': items_data,
        'customer': order.customer.name if order.customer else ''
    })






# -------------------------------------------------------------------
#     فایل برش
# -------------------------------------------------------------------
@login_required
@staff_or_representative_required
def export_autocut_xml(request, order_id):
    order = get_object_or_404(Order, pk=order_id)

    # فقط تسک‌های ایستگاه برش (cut) که وضعیت pending دارند
    cut_tasks = ProductionTask.objects.filter(
        order=order,
        station_name='cut',
        status='pending'
    ).select_related('part', 'part__material')

    # گروه‌بندی بر اساس متریال
    tasks_by_material = {}
    for task in cut_tasks:
        material = task.part.material
        if material not in tasks_by_material:
            tasks_by_material[material] = []
        tasks_by_material[material].append(task)

    # ایجاد ریشه XML با namespace
    NS = "http://www.King-stone.com"
    ET.register_namespace('', NS)
    root = ET.Element(f"{{{NS}}}AutoCUT", {"ver": "500"})

    project = ET.SubElement(root, f"{{{NS}}}Project", {
        "Name": "Project",
        "Selected": "0",
        "Update": "0",
        "DefaultLevel": "0",
        "UserFields": "F2,F3,F4,F5,F26,F18,F19,F20,",
        "FieldLabels": "TLGrain=دسته محصول,TLOrder=نام محصول,TLType=fcfffff,F2=نام قطعه,F3=بارکد,F4=نوار طول 1,F5=نوار طول 2,F26=نوار عرض1,F18=نوار عرض 2,F19=تحویل به,F20=نام مشتری,"
    })

    for material, tasks in tasks_by_material.items():
        # نام نوع ورق (مثلاً "gerdo-gerdo-gerdo-1") - می‌توانید از material.name یا فیلد دیگری استفاده کنید
        material_type = material.name.replace(' ', '-')  # یا هر منطق دلخواه

        data = ET.SubElement(project, f"{{{NS}}}Data", {
            "Class": "3",
            "TotalUnit": "1000000",
            "Type": material_type,
            "Ply": str(material.thickness)
        })

        objective = ET.SubElement(data, f"{{{NS}}}Objective", {"Type": "Shape", "Count": str(len(tasks))})

        for idx, task in enumerate(tasks, start=1):
            part = task.part
            # پیدا کردن OrderItem مربوطه برای گرفتن نام محصول و مشتری
            order_item = order.items.filter(product__bom__part=part).first()
            product_name = order_item.product.name if order_item else ""
            product_category = order_item.product.category if order_item else ""
            customer_name = order.user.username if order.user.username else ""

            shape = ET.SubElement(objective, f"{{{NS}}}Shape", {
                "Name": f"P{idx:03d}",
                "X": str(part.length),
                "Y": str(part.width),
                "Turn": "true" if part.turn else "false",
                "Grain": product_category or "",
                "Order": product_name,   
                "Count": str(task.quantity),
                "F2": part.f2 or part.name,
                "F3": part.f3 or "",
                "F4": part.f4 or "",
                "F5": part.f5 or "",
                "F26": part.f26 or "",
                "F18": part.f18 or "",
                "F19": part.routing_code or "",
                "F20": customer_name or "",
            })

    # تبدیل به رشته XML زیبا
    xml_str = ET.tostring(root, encoding='utf-16', method='xml')
    # minidom برای فرمت زیبا (اختیاری)
    # dom = minidom.parseString(xml_str)
    # pretty_xml = dom.toprettyxml(indent="  ")

    response = HttpResponse(xml_str, content_type='application/xml')
    response['Content-Disposition'] = f'attachment; filename="order_{order.id}_autocut.xml"'
    return response


@login_required
@staff_or_representative_required
def export_multiple_autocut(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    order_ids = request.POST.getlist('order_ids')
    if not order_ids:
        messages.error(request, "هیچ سفارشی انتخاب نشده است.")
        return redirect('order_list')

    orders = get_list_or_404(Order, pk__in=order_ids)

    # جمع‌آوری تمام تسک‌های برش (pending) برای سفارشات انتخاب‌شده
    cut_tasks = ProductionTask.objects.filter(
        order__in=orders,
        station_name='cut',
        status='pending'
    ).select_related('part', 'part__material', 'order', 'order__customer')

    if not cut_tasks.exists():
        messages.warning(request, "هیچ قطعه‌ای در انتظار برش برای سفارشات انتخاب‌شده یافت نشد.")
        return redirect('order_list')

    # گروه‌بندی بر اساس متریال
    tasks_by_material = {}
    for task in cut_tasks:
        material = task.part.material
        if material not in tasks_by_material:
            tasks_by_material[material] = []
        tasks_by_material[material].append(task)

    # ایجاد XML
    NS = "http://www.King-stone.com"
    ET.register_namespace('', NS)
    root = ET.Element(f"{{{NS}}}AutoCUT", {"ver": "500"})

    project = ET.SubElement(root, f"{{{NS}}}Project", {
        "Name": "MultipleOrders",
        "Selected": "0",
        "Update": "0",
        "DefaultLevel": "0",
        "UserFields": "F2,F3,F4,F5,F26,F18,F19,F20,",
        "FieldLabels" : "TLGrain=دسته محصول,TLOrder=نام محصول,TLType=fcfffff,F2=نام قطعه,F3=بارکد,F4=نوار طول 1,F5=نوار طول 2,F26=نوار عرض1,F18=نوار عرض 2,F19=تحویل به,F20=نام مشتری,"
        # "FieldLabels": "TLGrain=دسته محصول,TLOrder=شماره سفارش,TLType=fcfffff,F2=نام قطعه,F3=بارکد,F4=نوار طول 1,F5=نوار طول 2,F26=نوار عرض1,F18=نوار عرض 2,F19=تحویل به,F20=نام مشتری,"
    })

    shape_counter = 1
    for material, tasks in tasks_by_material.items():
        material_type = material.name.replace(' ', '-')
        data = ET.SubElement(project, f"{{{NS}}}Data", {
            "Class": "3",
            "TotalUnit": "1000000",
            "Type": material_type,
            "Ply": str(material.thickness)
        })

        objective = ET.SubElement(data, f"{{{NS}}}Objective", {
            "Type": "Shape",
            "Count": str(len(tasks))
        })

        for task in tasks:
            part = task.part
            order = task.order
            # پیدا کردن OrderItem مربوطه برای گرفتن نام محصول و مشتری
            order_item = order.items.filter(product__bom__part=part).first()
            product_name = order_item.pname if order_item else ""
            product_category = order_item.grain if order_item else ""
            customer_name = order.user.username if order.user.username else ""
            print( part.pname)
            
            shape = ET.SubElement(objective, f"{{{NS}}}Shape", {
                "Name":  f"P{shape_counter:03d}",
                "X": str(part.length),
                "Y": str(part.width),
                "Turn": "true" if part.turn else "false",
                # "Grian": product_category or "",
                # "Order": product_name or "",  
                "Grain": part.grain or product_category or "",
                "Order": part.pname,    
                "Count": str(task.quantity),
                "F2": part.f2 or part.name,
                "F3": part.f3 or "",
                "F4": part.f4 or "",
                "F5": part.f5 or "",
                "F26": part.f26 or "",
                "F18": part.f18 or "",
                "F19": part.routing_code or "",
                "F20": customer_name or "",
            })
            shape_counter += 1

    xml_str = ET.tostring(root, encoding='utf-16', method='xml')

    # *** به‌روزرسانی تسک‌ها با save() برای فعال‌سازی مرحله بعد ***
    with transaction.atomic():
        updated_count = 0
        for task in cut_tasks:
            task.status = 'done'
            task.scanned_by = request.user
            task.save()   # این متد مرحله بعد را فعال و وضعیت سفارش را به‌روز می‌کند
            updated_count += 1

    messages.success(
        request,
        f"فایل برش با موفقیت ایجاد و {updated_count} قطعه به‌عنوان انجام‌شده ثبت گردید. "
        "مراحل بعدی به‌طور خودکار فعال شدند."
    )

    response = HttpResponse(xml_str, content_type='application/xml')
    response['Content-Disposition'] = 'attachment; filename="batch_autocut.xml"'
    return response





# -------------------------------------------------------------------
#      
# -------------------------------------------------------------------
@login_required
@admin_or_manager_required
def upload_form(request):
    if request.method == 'POST':
        file = request.FILES.get('excel_file')
        if not file:
            messages.error(request, "فایلی انتخاب نشده است.")
            return redirect('upload_form')

        try:
            df = pd.read_excel(file)
        except Exception as e:
            messages.error(request, f"خطا در خواندن فایل: {e}")
            return redirect('upload_form')

        saved = 0
        for idx, row in df.iterrows():
            try:
                with transaction.atomic():
                    username = str(row.get('نام', '')).strip()
                    if not username:
                        username = f"user_{idx}"
                    user, _ = User.objects.get_or_create(username=username)

                    customer_name = str(row.get('مشتری', '')).strip()
                    if not customer_name:
                        customer_name = f"{idx}"
                    customer, _ = Customer.objects.get_or_create(
                        user=user,
                        name=customer_name
                    )
                    order_id = int(row.get('شماره'))
                    order, _ = Order.objects.get_or_create(
                        id=order_id,
                        defaults={'user': user, 'customer': customer }
                    )
                    category, _ = ProductCategory.objects.get_or_create(name=str(row.get('گروه محصول', '')).strip())
                    size_val = str(row.get('اندازه', ''))
                    if size_val in ['nan', 'استاندارد']:
                        size_val = ''
                    product, _ = Product.objects.get_or_create(
                        category=category,
                        name=str(row.get('محصول', '')).strip(),
                        # default_size=size_val
                    )
                    order_item = OrderItem.objects.create(
                        order=order,
                        id=idx+1,
                        product=product,
                        quantity=int(row.get('تعداد', 1)),
                        size=size_val,
                        notes=str(row.get('توضیحات', ''))
                    )

                    # رنگ‌ها
                    color_parts = ['بدنه', 'درب', 'دستگیره', 'پایه', 'صفحه']
                    for part in color_parts:
                        code = str(row.get(part, 'nan'))
                        if code and code != 'nan':
                            Color.objects.create(
                                part=part,
                                code=code.split('.')[0],
                                orderitem=order_item
                            )
                    saved += 1
            except Exception as e:
                logger.exception(f"خطا در ردیف {idx}: {e}")
                messages.error(request, f"خطا در ردیف {idx+1}: {e}")
        messages.success(request, f'{saved} سفارش با موفقیت ذخیره شد.')
        return redirect('upload_form')

    return render(request, 'upload.html')


# -------------------------------------------------------------------
#      اسکن قطعات
# -------------------------------------------------------------------
#      اسکن قطعات
# -------------------------------------------------------------------
@login_required
@staff_or_representative_required
def scan_part(request):
    # بررسی پروفایل کاربر
    if not hasattr(request.user, 'workerprofile'):
        messages.error(request, "")
        return redirect('dashboard')

    worker_stage = request.user.workerprofile.stage
    pending_tasks = ProductionTask.objects.filter(
        station_name=worker_stage,
        status='pending'
    ).select_related('part', 'order', 'order_item__product__category').order_by('order__created_at')

    if request.method == 'POST':
        barcode = request.POST.get('barcode', '').strip()
        task_id = request.POST.get('task_id')

        if task_id:
            task = get_object_or_404(pending_tasks, id=task_id)
            with transaction.atomic():
                task.status = 'done'
                task.scanned_by = request.user
                task.save()

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': True,
                    'status': 'done',
                    'message': f"تسک '{task.part.name}' تکمیل شد.",
                    'task_id': task.id,
                    'completed_quantity': task.completed_quantity,
                    'quantity': task.quantity,
                })
            if worker_stage == 'cnc':
                file_barcode = re.sub(r'\.item\d+$', '', task.part.f3)
                download_url = reverse('download_cnc_file', args=[file_barcode])
                messages.success(request, f"تسک '{task.part.name}' تکمیل شد. دریافت فایل...")
                return redirect(download_url)
            if worker_stage == 'dr':
                file_barcode = re.sub(r'\.item\d+$', '', task.part.f3)
                download_url = reverse('download_dr_file', args=[file_barcode])
                messages.success(request, f" '{task.part.name}' تکمیل شد. دریافت فایل سوراخکاری...")
                return redirect(download_url)
            messages.success(request, f" قطعه '{task.part.name}' با موفقیت تکمیل شد.")
            return redirect('scan_part')

        if barcode:
            clean_barcode = re.sub(r'\.cnc$', '', barcode, flags=re.IGNORECASE)
            clean_barcode = re.sub(r'\.scx$', '', clean_barcode, flags=re.IGNORECASE)
            try:
                part = Part.objects.get(f3=clean_barcode)
            except Part.DoesNotExist:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'success': False, 'error': f"قطعه‌ای با بارکد '{barcode}' یافت نشد."}, status=404)
                messages.error(request, f"قطعه‌ای با بارکد '{barcode}' یافت نشد.")
                return redirect('scan_part')

            task = pending_tasks.filter(part=part).first()
            if not task:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'success': False, 'error': f"هیچ  در انتظاری برای قطعه '{part.name}' در ایستگاه شما یافت نشد."}, status=404)
                messages.error(request, f"هیچ  در انتظاری برای قطعه '{part.name}' در ایستگاه شما یافت نشد.")
                return redirect('scan_part')

            with transaction.atomic():
                task.status = 'done'
                task.scanned_by = request.user
                task.save()

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': True,
                    'status': 'done',
                    'message': f"قطعه '{part.name}' تکمیل شد.",
                    'task_id': task.id,
                    'completed_quantity': task.completed_quantity,
                    'quantity': task.quantity,
                })
            messages.success(request, f"قطعه '{part.name}' (بارکد: {barcode}) تکمیل شد.")
            return redirect('scan_part')

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': 'لطفاً بارکد را وارد کنید.'}, status=400)
        messages.error(request, "لطفاً بارکد را وارد کنید یا یکی از قطعه را انتخاب نمایید.")
        return redirect('scan_part')

    # GET request
    stage_display = dict(STATION_CHOICES).get(worker_stage, worker_stage)

    items_with_progress = []
    item_ids_seen = set()
    for task in pending_tasks:
        item = task.order_item
        if not item or item.id in item_ids_seen:
            continue
        item_ids_seen.add(item.id)
        station_tasks = item.paint_tasks.filter(station_name=worker_stage)
        items_with_progress.append({
            'item': item,
            'order': item.order,
            'total_parts': station_tasks.count(),
            'remaining_parts': station_tasks.exclude(status='done').count(),
        })

    first_item_tasks = []
    if pending_tasks:
        first = pending_tasks.first()
        if first.order_item_id:
            from .utils import get_item_task_progress_for_station
            first_item_tasks = get_item_task_progress_for_station(first.order_item_id, worker_stage)

    context = {
        'pending_tasks': pending_tasks,
        'items_with_progress': items_with_progress,
        'worker_stage': worker_stage,
        'stage_display': stage_display,
        'item_tasks_json': json.dumps(first_item_tasks, ensure_ascii=False),
        'item_order_id': pending_tasks.first().order_id if pending_tasks else None,
        'item_order_item_id': pending_tasks.first().order_item_id if pending_tasks else None,
    }
    return render(request, 'scan_part.html', context)


@login_required
@staff_or_representative_required
def scan_part_cnc(request):
    if request.method != 'POST':
        return redirect('scan_part')

    barcode = request.POST.get('barcode', '').strip()
    if not barcode:
        return JsonResponse({'success': False, 'error': 'بارکد خالی است.'}, status=400)

    clean_barcode = re.sub(r'\.cnc$', '', barcode, flags=re.IGNORECASE)
    part = Part.objects.filter(f3=clean_barcode).first()
    if not part:
        return JsonResponse({'success': False, 'error': f"قطعه‌ای با بارکد '{barcode}' یافت نشد."}, status=404)

    if not hasattr(request.user, 'workerprofile'):
        return JsonResponse({'success': False, 'error': 'پروفایل کاری شما تعریف نشده است.'}, status=403)

    worker_stage = request.user.workerprofile.stage
    if worker_stage != 'cnc':
        return JsonResponse({'success': False, 'error': 'شما مجاز به اسکن در ایستگاه CNC نیستید.'}, status=403)

    task = ProductionTask.objects.filter(
        part=part,
        station_name='cnc',
        status='pending'
    ).first()

    if not task:
        return JsonResponse({'success': False, 'error': f"هیچ  در انتظاری برای قطعه '{part.name}' در ایستگاه CNC یافت نشد."}, status=404)

    is_first_scan = task.completed_quantity == 0

    with transaction.atomic():
        task.completed_quantity += 1
        if task.completed_quantity > task.quantity:
            task.completed_quantity = task.quantity
        if task.completed_quantity >= task.quantity:
            task.status = 'done'
        task.scanned_by = request.user
        task.save()

    from .utils import get_item_task_progress_for_station
    response_data = {
        'success': True,
        'status': task.status,
        'message': f"✅  CNC: {task.completed_quantity} از {task.quantity} ثبت شد.",
        'task_id': task.id,
        'order_id': task.order_id,
        'completed_quantity': task.completed_quantity,
        'quantity': task.quantity,
        'item_tasks': get_item_task_progress_for_station(task.order_item_id, 'cnc'),
    }

    if is_first_scan:
        source_dir = getattr(settings, 'CNC_SOURCE_DIR', '')
        extension = getattr(settings, 'CNC_FILE_EXTENSION', '.cnc')
        file_barcode = re.sub(r'\.item\d+$', '', part.f3)
        filename = f"{file_barcode}{extension}"
        file_path = os.path.join(source_dir, filename)
        download_url = reverse('download_cnc_file', args=[file_barcode])
        response_data['download_url'] = download_url
        if not os.path.exists(file_path):
            response_data['message'] = f"✅  CNC تکمیل شد، اما فایل '{filename}' در سرور یافت نشد."

    return JsonResponse(response_data)


@login_required
@staff_or_representative_required
def download_cnc_file(request, barcode):
    """
    دانلود فایل CNC با بارکد داده شده.
    بارکد می‌تواند شامل .itemX باشد یا نباشد؛ جستجوی قطعه با startswith انجام می‌شود.
    """
    clean_barcode = re.sub(r'\.cnc$', '', barcode, flags=re.IGNORECASE)

    # یافتن قطعه‌ای که f3 آن با این بارکد شروع شود
    part = Part.objects.filter(f3__startswith=clean_barcode).first()
    if not part:
        raise Http404(f"قطعه‌ای با بارکد '{barcode}' یافت نشد.")

    # حذف .itemX برای ساخت نام فایل
    file_barcode = re.sub(r'\.item\d+$', '', clean_barcode)
    source_dir = getattr(settings, 'CNC_SOURCE_DIR', '')
    extension = getattr(settings, 'CNC_FILE_EXTENSION', '.cnc')
    filename = f"{file_barcode}{extension}"
    file_path = os.path.join(source_dir, filename)

    if not os.path.exists(file_path):
        raise Http404(f"فایل CNC با نام '{filename}' یافت نشد.")

    with open(file_path, 'rb') as f:
        response = HttpResponse(f.read(), content_type='application/octet-stream')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

@login_required
@staff_or_representative_required
def scan_part_dr(request):
    if request.method != 'POST':
        return redirect('scan_part')

    barcode = request.POST.get('barcode', '').strip()
    if not barcode:
        return JsonResponse({'success': False, 'error': 'بارکد خالی است.'}, status=400)

    clean_barcode = re.sub(r'\.(scx)$', '', barcode, flags=re.IGNORECASE)
    part = Part.objects.filter(f3=clean_barcode).first()
    if not part:
        return JsonResponse({'success': False, 'error': f"قطعه‌ای با بارکد '{barcode}' یافت نشد."}, status=404)

    if not hasattr(request.user, 'workerprofile'):
        return JsonResponse({'success': False, 'error': 'پروفایل کاری شما تعریف نشده است.'}, status=403)

    worker_stage = request.user.workerprofile.stage
    if worker_stage != 'dr':
        return JsonResponse({'success': False, 'error': 'شما مجاز به اسکن در ایستگاه سوراخکاری نیستید.'}, status=403)

    task = ProductionTask.objects.filter(
        part=part,
        station_name='dr',
        status='pending'
    ).first()

    if not task:
        return JsonResponse({'success': False, 'error': f"هیچ  در انتظاری برای قطعه '{part.name}' در ایستگاه سوراخکاری یافت نشد."}, status=404)

    is_first_scan = task.completed_quantity == 0

    with transaction.atomic():
        task.completed_quantity += 1
        if task.completed_quantity > task.quantity:
            task.completed_quantity = task.quantity
        if task.completed_quantity >= task.quantity:
            task.status = 'done'
        task.scanned_by = request.user
        task.save()

    from .utils import get_item_task_progress_for_station
    response_data = {
        'success': True,
        'status': task.status,
        'message': f"✅  سوراخکاری: {task.completed_quantity} از {task.quantity} ثبت شد.",
        'task_id': task.id,
        'order_id': task.order_id,
        'completed_quantity': task.completed_quantity,
        'quantity': task.quantity,
        'item_tasks': get_item_task_progress_for_station(task.order_item_id, 'dr'),
    }

    if is_first_scan:
        source_dir = getattr(settings, 'DR_SOURCE_DIR', '')
        extension = getattr(settings, 'DR_FILE_EXTENSION', '.scx')
        file_barcode = re.sub(r'\.item\d+$', '', part.f3)
        filename = f"{file_barcode}{extension}"
        file_path = os.path.join(source_dir, filename)
        download_url = reverse('download_dr_file', args=[file_barcode])
        response_data['download_url'] = download_url
        if not os.path.exists(file_path):
            response_data['message'] = f"✅  سوراخکاری تکمیل شد، اما فایل '{filename}' در سرور یافت نشد."

    return JsonResponse(response_data)


@login_required
@staff_or_representative_required
def download_dr_file(request, barcode):
    """
    دانلود فایل XML سوراخکاری با بارکد داده شده.
    """
    clean_barcode = re.sub(r'\.(scx)$', '', barcode, flags=re.IGNORECASE)

    part = Part.objects.filter(f3__startswith=clean_barcode).first()
    if not part:
        raise Http404(f"قطعه‌ای با بارکد '{barcode}' یافت نشد.")

    file_barcode = re.sub(r'\.item\d+$', '', clean_barcode)
    source_dir = getattr(settings, 'DR_SOURCE_DIR', '')
    extension = getattr(settings, 'DR_FILE_EXTENSION', '.xml')
    filename = f"{file_barcode}{extension}"
    file_path = os.path.join(source_dir, filename)

    if not os.path.exists(file_path):
        raise Http404(f"فایل سوراخکاری با نام '{filename}' یافت نشد.")

    with open(file_path, 'rb') as f:
        response = HttpResponse(f.read(), content_type='application/xml')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        # response['Content-Disposition'] = f'attachment; filename="{barcode}"'
       
        return response


# -------------------------------------------------------------------
#     گزارش  تولید
# -------------------------------------------------------------------

@login_required
@admin_or_manager_required
@staff_or_representative_required
def report_orders(request):
    data = Order.objects.values('status').annotate(count=Count('id'))
    return render(request, 'reports/orders.html', {'data': data})


def _parse_jalali_or_none(date_str):
    """Parse a Jalali date string (YYYY/MM/DD or YYYY-MM-DD) into a jdatetime.date.

    PersianDateField.get_prep_value only converts to Gregorian when it receives
    an actual ``jdatetime.date`` — a raw string would bypass that conversion and
    silently break database lookups against PersianDateField columns.
    """
    if not date_str:
        return None
    try:
        y, m, d = map(int, date_str.strip().replace('/', '-').split('-'))
        return jdatetime.date(y, m, d)
    except (ValueError, TypeError):
        return None


@login_required
@staff_or_representative_required
def report_stages(request):
    # ---------- base queryset ----------
    base_items = OrderItem.objects.select_related(
        'order__user', 'product__category'
    )

    # ---------- text search ----------
    q = request.GET.get('q')
    if q:
        base_items = base_items.filter(
            Q(order__id__icontains=q) |
            Q(product__name__icontains=q) |
            Q(order__customer__name__icontains=q) |
            Q(order__user__username__icontains=q)
        )

    # ---------- filters ----------
    representative_id = request.GET.get('representative')
    if representative_id:
        base_items = base_items.filter(order__user_id=representative_id)

    category_id = request.GET.get('category')
    if category_id:
        base_items = base_items.filter(product__category_id=category_id)

    product_id = request.GET.get('product')
    if product_id:
        base_items = base_items.filter(product_id=product_id)

    date_target = request.GET.get('date_target', 'order')
    date_from_raw = request.GET.get('date_from', '')
    date_to_raw = request.GET.get('date_to', '')
    date_from = _parse_jalali_or_none(date_from_raw)
    date_to = _parse_jalali_or_none(date_to_raw)

    # Order.created_at is a PersianDateField (handles jdatetime.date natively).
    # PackagingUnit.packed_at / shipped_at are DateTimeField -- pass Gregorian date.
    if date_from:
        if date_target == 'pack':
            base_items = base_items.filter(packaging_units__packed_at__date__gte=date_from.togregorian())
        elif date_target == 'ship':
            base_items = base_items.filter(packaging_units__shipped_at__date__gte=date_from.togregorian())
        else:
            base_items = base_items.filter(order__created_at__gte=date_from)
    if date_to:
        if date_target == 'pack':
            base_items = base_items.filter(packaging_units__packed_at__date__lte=date_to.togregorian())
        elif date_target == 'ship':
            base_items = base_items.filter(packaging_units__shipped_at__date__lte=date_to.togregorian())
        else:
            base_items = base_items.filter(order__created_at__lte=date_to)

    # ---------- summary (based on filtered base_items before stage filters) ----------
    total = base_items.count()
    # Single aggregated query instead of one COUNT per station (11 → 1)
    stage_done_counts = (
        ProductionLog.objects
        .filter(order_item_id__in=base_items.values('pk'))
        .values('stage')
        .annotate(done=Count('order_item_id', distinct=True))
    )
    done_map = {row['stage']: row['done'] for row in stage_done_counts}

    summary = {}
    for code, name in STATION_CHOICES:
        summary[code] = {'name': name, 'done': done_map.get(code, 0), 'total': total}

    # ---------- stage filters (excluding packaging & shipping) ----------
    stage_pending = request.GET.get('stage_pending')
    stage_done = request.GET.get('stage_done')
    items = base_items
    if stage_pending and stage_pending in dict(STATION_CHOICES):
        items = items.exclude(logs__stage=stage_pending)
    if stage_done and stage_done in dict(STATION_CHOICES):
        items = items.filter(logs__stage=stage_done)

    # ---------- packaging / shipping filters ----------
    packaging_status = request.GET.get('packaging_status')
    shipping_status = request.GET.get('shipping_status')

    # We'll build annotations using subqueries for precise calculations
    pack_units = PackagingUnit.objects.filter(order_item=OuterRef('pk'))
    ship_units = PackagingUnit.objects.filter(order_item=OuterRef('pk'))
    packed_not_shipped_units = PackagingUnit.objects.filter(
        order_item=OuterRef('pk'), is_packed=True, is_shipped=False
    )

    items = items.annotate(
        total_units=Count('packaging_units'),
        packed_count=Subquery(
            pack_units.filter(is_packed=True).values('order_item')
            .annotate(cnt=Count('id')).values('cnt'),
            output_field=IntegerField()
        ),
        shipped_count=Subquery(
            ship_units.filter(is_shipped=True).values('order_item')
            .annotate(cnt=Count('id')).values('cnt'),
            output_field=IntegerField()
        ),
        in_stock_count=Subquery(
            packed_not_shipped_units.values('order_item')
            .annotate(cnt=Count('id')).values('cnt'),
            output_field=IntegerField()
        ),
    )

    # Apply packaging filter
    if packaging_status == 'done':
        items = items.filter(total_units__gt=0, packed_count__gte=F('total_units'))
    elif packaging_status == 'packed':
        items = items.filter(packed_count__gt=0)
    elif packaging_status == 'pending':
        items = items.filter(total_units__gt=0, packed_count__lt=F('total_units'))
    elif packaging_status == 'none':
        items = items.filter(total_units=0)
    elif packaging_status == 'in_stock':
        items = items.filter(in_stock_count__gt=0)

    # Apply shipping filter
    if shipping_status == 'done':
        items = items.filter(total_units__gt=0, shipped_count__gte=F('total_units'))
    elif shipping_status == 'pending':
        items = items.filter(total_units__gt=0, shipped_count__lt=F('total_units'))
    elif shipping_status == 'none':
        items = items.filter(total_units=0)

    # ---------- build report data ----------
    # Pagination must come AFTER the annotation-based packaging/shipping
    # filters, but the annotations themselves stay on the queryset so the
    # filtering keeps working. Only the *display* loop runs on the slice.
    items = items.order_by('-id')
    paginator = Paginator(items, 200)
    page_number = request.GET.get('page')
    items_page = paginator.get_page(page_number)
    items_list = list(items_page)
    item_ids = [item.id for item in items_list]

    # --- batch-fetch production logs for the current page (1 query) ---
    # .values('created_at') returns the PersianDateField as a jdatetime.date,
    # so .month/.day yield the Shamsi components exactly like the model access.
    logs_qs = (
        ProductionLog.objects
        .filter(order_item_id__in=item_ids)
        .order_by('id')
        .values('order_item_id', 'stage', 'created_at')
    )
    log_map = {}
    for row in logs_qs:
        per_stage = log_map.setdefault(row['order_item_id'], {})
        if row['stage'] not in per_stage:  # keep first (matches .first() semantics)
            ct = row['created_at']
            per_stage[row['stage']] = f"{ct.month:02d}/{ct.day:02d}" if ct else None

    # --- batch-fetch packaging stats for the current page (1 query) ---
    pack_stats = (
        PackagingUnit.objects
        .filter(order_item_id__in=item_ids)
        .values('order_item_id')
        .annotate(
            total=Count('id'),
            packed=Count('id', filter=Q(is_packed=True)),
            shipped=Count('id', filter=Q(is_shipped=True)),
            in_stock=Count('id', filter=Q(is_packed=True, is_shipped=False)),
        )
    )
    pack_map = {row['order_item_id']: row for row in pack_stats}

    report_data = []
    for item in items_list:
        item_logs = log_map.get(item.id, {})
        stage_status = {}
        for code, name in STATION_CHOICES:
            stage_status[code] = item_logs.get(code)

        stats = pack_map.get(item.id)
        if stats:
            total_units = stats['total']
            packed_units = stats['packed']
            shipped_units = stats['shipped']
            in_stock_units = stats['in_stock']
        else:
            total_units = 0
            packed_units = 0
            shipped_units = 0
            in_stock_units = 0

        report_data.append({
            'item': item,
            'stage_status': stage_status,
            'total_units': total_units,
            'packed_units': packed_units,
            'shipped_units': shipped_units,
            'in_stock_units': in_stock_units,
            'representative': (
                item.order.user.get_full_name() or item.order.user.username
            ) if item.order.user else '—',
            'category_name': item.product.category.name,
        })

    # ---------- dropdown lists ----------
    representatives = User.objects.filter(order__isnull=False).distinct().order_by('username')
    categories = ProductCategory.objects.all()
    if category_id:
        products = Product.objects.filter(category_id=category_id).order_by('name')
    else:
        products = Product.objects.none()

    # ---------- inventory low-stock count (for the quick-link badge) ----------
    from inventory.models import RawMaterial
    from django.db.models import Case, When, DecimalField, Sum

    low_stock_count = RawMaterial.objects.filter(is_active=True).annotate(
        stock=Sum(
            Case(
                When(movements__movement_type='consumption', then=-F('movements__quantity')),
                default=F('movements__quantity'),
                output_field=DecimalField(),
            )
        )
    ).filter(
        Q(stock__lte=F('min_stock_alert')) | Q(stock__isnull=True)
    ).count()

    context = {
        'report_data': report_data,
        'page_obj': items_page,
        'station_choices': STATION_CHOICES,
        'summary': summary,
        'search_query': q,
        'representatives': representatives,
        'categories': categories,
        'products': products,
        'selected_representative': representative_id,
        'selected_category': category_id,
        'selected_product': product_id,
        'date_from': date_from_raw,
        'date_to': date_to_raw,
        'stage_pending': stage_pending,
        'stage_done': stage_done,
        'packaging_status': packaging_status or '',
        'shipping_status': shipping_status or '',
        'date_target': date_target,
        'low_stock_count': low_stock_count,
    }
    return render(request, 'reports/stages.html', context)


@login_required
@staff_or_representative_required
def report_production_unified(request):
    """
    گزارش یکپارچه تولید بر اساس ProductionEvent.
    فیلترها مشابه report_stages: q, category, product, date_from, date_to
    خروجی: برای هر order_item، آخرین رویداد هر ایستگاه + وضعیت فعلی تسک‌ها.
    """
    from .models import ProductionEvent, OrderItem
    from django.db.models import Q, OuterRef, Subquery, Count, F

    def _parse_jalali_date_str(date_str):
        """Convert Persian date string YYYY-MM-DD or YYYY/MM/DD to Gregorian date object or None."""
        if not date_str:
            return None
        try:
            # normalize separators
            normalized = date_str.replace('/', '-')
            y, m, d = map(int, normalized.split('-'))
            persian_date = jdatetime.date(y, m, d)
            return persian_date.togregorian()
        except (ValueError, TypeError):
            return None

    events = ProductionEvent.objects.filter(event_type='done').select_related(
        'order_item__product__category', 'order_item__order__user', 'task'
    )

    q = request.GET.get('q')
    if q:
        events = events.filter(
            Q(order__id__icontains=q) |
            Q(order_item__product__name__icontains=q) |
            Q(order_item__order__customer__name__icontains=q)
        )

    date_from_str = request.GET.get('date_from')
    date_from = _parse_jalali_date_str(date_from_str)
    if date_from:
        events = events.filter(created_at__date__gte=date_from)
    date_to_str = request.GET.get('date_to')
    date_to = _parse_jalali_date_str(date_to_str)
    if date_to:
        events = events.filter(created_at__date__lte=date_to)

    category_id = request.GET.get('category')
    if category_id:
        events = events.filter(order_item__product__category_id=category_id)

    product_id = request.GET.get('product')
    if product_id:
        events = events.filter(order_item__product_id=product_id)

    representative_id = request.GET.get('representative')
    if representative_id:
        events = events.filter(order__user_id=representative_id)

    item_station_latest = {}
    for ev in events.order_by('created_at'):
        key = (ev.order_item_id, ev.station_name)
        item_station_latest[key] = ev

    item_ids = {k[0] for k in item_station_latest if k[0]}
    items = OrderItem.objects.filter(id__in=item_ids).select_related(
        'order__user', 'product__category'
    ).prefetch_related('logs', 'packaging_units')

    # فیلترهای بازه زمانی برای بسته‌بندی و ارسال
    pack_date_from_str = request.GET.get('pack_date_from')
    pack_date_from = _parse_jalali_date_str(pack_date_from_str)
    if pack_date_from:
        items = items.filter(packaging_units__packed_at__date__gte=pack_date_from)
    pack_date_to_str = request.GET.get('pack_date_to')
    pack_date_to = _parse_jalali_date_str(pack_date_to_str)
    if pack_date_to:
        items = items.filter(packaging_units__packed_at__date__lte=pack_date_to)
    ship_date_from_str = request.GET.get('ship_date_from')
    ship_date_from = _parse_jalali_date_str(ship_date_from_str)
    if ship_date_from:
        items = items.filter(packaging_units__shipped_at__date__gte=ship_date_from)
    ship_date_to_str = request.GET.get('ship_date_to')
    ship_date_to = _parse_jalali_date_str(ship_date_to_str)
    if ship_date_to:
        items = items.filter(packaging_units__shipped_at__date__lte=ship_date_to)

    # فیلتر وضعیت بسته‌بندی و ارسال
    packaging_status = request.GET.get('packaging_status')
    if packaging_status:
        items = items.annotate(
            total_pack=Count('packaging_units', distinct=True),
            packed_count=Count('packaging_units', filter=Q(packaging_units__is_packed=True), distinct=True),
        )
        if packaging_status == 'done':
            items = items.filter(total_pack__gt=0, packed_count=F('total_pack'))
        elif packaging_status == 'pending':
            items = items.filter(total_pack__gt=0, packed_count__lt=F('total_pack'))
        elif packaging_status == 'none':
            items = items.filter(total_pack=0)

    shipping_status = request.GET.get('shipping_status')
    if shipping_status:
        items = items.annotate(
            total_pack_ship=Count('packaging_units', distinct=True),
            shipped_count=Count('packaging_units', filter=Q(packaging_units__is_shipped=True), distinct=True),
        )
        if shipping_status == 'done':
            items = items.filter(total_pack_ship__gt=0, shipped_count=F('total_pack_ship'))
        elif shipping_status == 'pending':
            items = items.filter(total_pack_ship__gt=0, shipped_count__lt=F('total_pack_ship'))
        elif shipping_status == 'none':
            items = items.filter(total_pack_ship=0)

    report_rows = []
    for item in items:
        stage_status = {}
        for code, name in STATION_CHOICES:
            ev = item_station_latest.get((item.id, code))
            if ev and ev.created_at:
                jdate = ev.created_at
                if hasattr(jdate, 'strftime'):
                    stage_status[code] = f"{jdate.month:02d}/{jdate.day:02d}"
                else:
                    stage_status[code] = str(jdate)
            else:
                log = item.logs.filter(stage=code).first()
                if log and log.created_at:
                    jdate = log.created_at
                    stage_status[code] = f"{jdate.month:02d}/{jdate.day:02d}"
                else:
                    stage_status[code] = None

        total_units = item.packaging_units.count()
        packed_units = item.packaging_units.filter(is_packed=True).count()
        shipped_units = item.packaging_units.filter(is_shipped=True).count()

        report_rows.append({
            'item': item,
            'stage_status': stage_status,
            'total_units': total_units,
            'packed_units': packed_units,
            'shipped_units': shipped_units,
            'representative': item.order.user.get_full_name() or item.order.user.username,
            'category_name': item.product.category.name if item.product.category else '',
        })

    representatives = User.objects.filter(order__isnull=False).distinct().order_by('username')
    categories = ProductCategory.objects.all()
    if category_id:
        products = Product.objects.filter(category_id=category_id).order_by('name')
    else:
        products = Product.objects.none()

    context = {
        'report_rows': report_rows,
        'station_choices': STATION_CHOICES,
        'search_query': q,
        'date_from': date_from_str,
        'date_to': date_to_str,
        'pack_date_from': pack_date_from_str,
        'pack_date_to': pack_date_to_str,
        'ship_date_from': ship_date_from_str,
        'ship_date_to': ship_date_to_str,
        'packaging_status': packaging_status,
        'shipping_status': shipping_status,
        'representatives': representatives,
        'categories': categories,
        'products': products,
        'selected_representative': representative_id,
        'selected_category': category_id,
        'selected_product': product_id,
    }
    return render(request, 'reports/production_unified.html', context)


@login_required
@admin_or_manager_required
@staff_or_representative_required
def report_workers(request):
    data = ProductionLog.objects.values('user__username').annotate(count=Count('id'))
    return render(request, 'reports/workers.html', {'data': data})
@login_required
@admin_or_manager_required
@staff_or_representative_required
def delayed_orders(request):
    limit = timezone.now() - timedelta(days=3)
    orders = Order.objects.filter(created_at__lt=limit).exclude(status='completed')
    return render(request, 'reports/delayed.html', {'orders': orders})


@login_required
@admin_or_manager_required
def report_material_consumption(request):
    from inventory.models import StockMovement, MaterialIssue
    from .models import ProductionDefect, PaintingProcess
    from django.db.models import Q
    from .utils import get_painting_process_for_color

    date_from_raw = request.GET.get('date_from')
    date_to_raw = request.GET.get('date_to')
    date_from = _parse_jalali_or_none(date_from_raw)
    date_to = _parse_jalali_or_none(date_to_raw)
    process_id = request.GET.get('process')
    rokeshi_filter = request.GET.get('rokeshi')

    ROKESHI_CODES = {'8', '9', '10', '11'}

    movements = StockMovement.objects.filter(
        movement_type='consumption',
    ).filter(
        Q(reference_task__isnull=False) | Q(reference_order_item__isnull=False)
    ).exclude(
        fulfilled_issue__purpose='rework'
    ).select_related(
        'raw_material',
        'reference_task__painting_stage__process',
        'reference_task__order_item__product__category',
        'reference_task__order_item',
        'reference_order_item__product__category',
        'reference_order_item__order',
        'reference_order_item',
    )

    if date_from:
        movements = movements.filter(created_at__date__gte=date_from.togregorian())
    if date_to:
        movements = movements.filter(created_at__date__lte=date_to.togregorian())
    if process_id:
        movements = movements.filter(
            Q(reference_task__painting_stage__process_id=process_id) |
            Q(reference_order_item__isnull=False, reference_order_item__product__isnull=False)  # will filter below
        )

    rows = {}

    def _row_key(item, process, packaging_unit=None):
        return (item.product_id, process.id if process else None, packaging_unit.id if packaging_unit else None)

    def _get_row(item, process, packaging_unit=None):
        key = _row_key(item, process, packaging_unit)
        return rows.setdefault(key, {
            'product': item.product,
            'product_category': item.product.category if item.product.category else None,
            'order': item.order,
            'order_item': item,
            'packaging_unit': packaging_unit,
            'process': process,
            'materials': {}, 'defect_count': 0, 'defect_materials': {},
        })

    def _is_rokeshi(item, color_part):
        if not color_part:
            return False
        color_obj = item.ordercolor.filter(part=color_part).first()
        code = color_obj.code if color_obj else None
        if not code or code == 'nan':
            from .utils import _parse_default_colors
            code = _parse_default_colors(item.product).get(color_part)
        return str(code) in ROKESHI_CODES

    for mv in movements:
        if mv.reference_task_id:
            task = mv.reference_task
            item = task.order_item
            if not item:
                continue
            stage = task.painting_stage
            process = stage.process if stage else None

            if task.station_name == 'paint' and rokeshi_filter:
                is_rok = _is_rokeshi(item, task.color_part)
                if rokeshi_filter == 'rokeshi' and not is_rok:
                    continue
                if rokeshi_filter == 'poshshi' and is_rok:
                    continue
        else:
            item = mv.reference_order_item
            if not item:
                continue
            # For paint materials, get process from color code
            color_part = mv.reference_color_part
            color_obj = item.ordercolor.filter(part=color_part).first()
            code = color_obj.code if color_obj and color_obj.code and color_obj.code != 'nan' else None
            if not code:
                from .utils import _parse_default_colors
                code = _parse_default_colors(item.product).get(color_part)
            process = get_painting_process_for_color(code) if code else None
            
            if process_id and (not process or str(process.id) != str(process_id)):
                continue

            if rokeshi_filter:
                is_rok = _is_rokeshi(item, color_part)
                if rokeshi_filter == 'rokeshi' and not is_rok:
                    continue
                if rokeshi_filter == 'poshshi' and is_rok:
                    continue

        row = _get_row(item, process)
        m = row['materials'].setdefault(
            mv.raw_material_id, {'raw_material': mv.raw_material, 'qty': Decimal('0')}
        )
        m['qty'] += mv.quantity

    defects = ProductionDefect.objects.select_related(
        'order_item__product', 'packaging_unit__order_item__product', 'task__painting_stage__process'
    ).prefetch_related('material_issues__raw_material')
    if date_from:
        defects = defects.filter(created_at__date__gte=date_from.togregorian())
    if date_to:
        defects = defects.filter(created_at__date__lte=date_to.togregorian())

    def _get_defect_process(defect):
        if defect.task and defect.task.painting_stage:
            return defect.task.painting_stage.process

        item = defect.packaging_unit.order_item if defect.packaging_unit_id else defect.order_item
        color_part = defect.color_part or (defect.task.color_part if defect.task else '')
        if not item or not color_part:
            return None

        color_obj = item.ordercolor.filter(part=color_part).first()
        code = color_obj.code if color_obj and color_obj.code and color_obj.code != 'nan' else None
        if not code:
            code = _parse_default_colors(item.product).get(color_part)
        return get_painting_process_for_color(code) if code else None

    for d in defects:
        item = d.packaging_unit.order_item if d.packaging_unit_id else d.order_item
        if not item:
            continue
        process = _get_defect_process(d)
        if process_id and (not process or str(process.id) != str(process_id)):
            continue

        color_part = d.color_part or (d.task.color_part if d.task else '')
        if rokeshi_filter:
            is_rok = _is_rokeshi(item, color_part)
            if rokeshi_filter == 'rokeshi' and not is_rok:
                continue
            if rokeshi_filter == 'poshshi' and is_rok:
                continue

        row = _get_row(item, process, d.packaging_unit)
        row['defect_count'] += d.quantity
        for issue in d.material_issues.all():
            if issue.issued_quantity <= 0:
                continue
            dm = row['defect_materials'].setdefault(
                issue.raw_material_id, {'raw_material': issue.raw_material, 'qty': Decimal('0')}
            )
            dm['qty'] += issue.issued_quantity

    report_rows = sorted(rows.values(), key=lambda r: (
        r['product_category'].name if r['product_category'] else '',
        r['product'].name,
        r['order'].number if r['order'] else '',
        r['order_item'].id if r['order_item'] else 0,
        r['packaging_unit'].unit_number if r['packaging_unit'] else 0,
        r['process'].name if r['process'] else ''
    ))

    material_totals = {}
    for row in report_rows:
        for mat in row['materials'].values():
            rm = mat['raw_material']
            material_totals.setdefault(rm.id, {'raw_material': rm, 'qty': Decimal('0')})
            material_totals[rm.id]['qty'] += mat['qty']
        for mat in row['defect_materials'].values():
            rm = mat['raw_material']
            material_totals.setdefault(rm.id, {'raw_material': rm, 'qty': Decimal('0')})
            material_totals[rm.id]['qty'] += mat['qty']
    
    material_totals_list = sorted(material_totals.values(), key=lambda m: m['raw_material'].name)

    defect_material_totals = {}
    for row in report_rows:
        for mat in row['defect_materials'].values():
            rm = mat['raw_material']
            defect_material_totals.setdefault(rm.id, {'raw_material': rm, 'qty': Decimal('0')})
            defect_material_totals[rm.id]['qty'] += mat['qty']
    defect_material_totals_list = sorted(
        defect_material_totals.values(), key=lambda m: m['raw_material'].name
    )

    context = {
        'report_rows': report_rows,
        'material_totals': material_totals_list,
        'defect_material_totals': defect_material_totals_list,
        'processes': PaintingProcess.objects.filter(is_active=True).order_by('name'),
        'date_from': date_from_raw or '', 'date_to': date_to_raw or '',
        'selected_process': process_id or '', 'rokeshi_filter': rokeshi_filter or '',
    }
    return render(request, 'reports/material_consumption.html', context)


# -------------------------------------------------------------------
#      ثبت  سفارش
# -------------------------------------------------------------------

@login_required
@admin_or_manager_required
def create_order(request):
    if request.method == 'POST':
        form = OrderCustomerForm(request.POST, user=request.user)
        item_form = OrderItemForm(request.POST)
        color_form = ColorSelectionForm(request.POST)
        if form.is_valid() and item_form.is_valid() and color_form.is_valid():
            with transaction.atomic():
                if form.is_admin and form.cleaned_data.get('representative'):
                    representative = form.cleaned_data['representative']
                else:
                    representative = request.user

                customer = form.cleaned_data['customer']
                if not customer:
                    customer = Customer.objects.create(
                        user=representative,
                        name=form.cleaned_data['new_customer_name'],
                        phone=form.cleaned_data.get('new_customer_phone', ''),
                        address=form.cleaned_data.get('new_customer_address', '')
                    )

                order = Order.objects.create(
                    user=representative,
                    customer=customer,
                    number=form.cleaned_data.get('number', ''),
                    status='draft'
                )

                product = item_form.cleaned_data['product']
                order_item = item_form.save(commit=False)
                order_item.order = order
                order_item.product = product
                order_item.unit_price = product.base_price
                order_item.save()

                for part_value, _ in Color.PART_CHOICES:
                    code = color_form.cleaned_data.get(f'color_{part_value}')
                    if code:
                        Color.objects.create(
                            part=part_value,
                            code=code,
                            orderitem=order_item
                        )

            messages.success(request, f"سفارش شماره {order.id} برای مشتری {customer.name} (نماینده: {representative.username}) ایجاد شد.")
            if 'add_another' in request.POST:
                return redirect('create_order_step2', order_id=order.id)
            return redirect('order_detail', order_id=order.id)
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"خطا در {field}: {error}")
            for field, errors in item_form.errors.items():
                for error in errors:
                    messages.error(request, f"خطا در {field}: {error}")
            for field, errors in color_form.errors.items():
                for error in errors:
                    messages.error(request, f"خطا در رنگ‌ها: {error}")
    else:
        form = OrderCustomerForm(user=request.user)
        item_form = OrderItemForm()
        color_form = ColorSelectionForm()

    context = {
        'form': form,
        'item_form': item_form,
        'color_form': color_form,
        'is_admin': form.is_admin,
    }
    return render(request, 'orders/create_order.html', context)


@login_required
@staff_or_representative_required
def ajax_load_customers(request):
    """بارگذاری مشتریان بر اساس نماینده انتخاب‌شده (برای ادمین)"""
    user_id = request.GET.get('representative')
    if user_id:
        customers = Customer.objects.filter(user_id=user_id).order_by('name')
    else:
        customers = Customer.objects.none()
    data = [{'id': c.id, 'name': c.name} for c in customers]
    return JsonResponse(data, safe=False)


@login_required
@admin_or_manager_required
def create_order_step2(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    existing_items = order.items.select_related('product__category').prefetch_related('ordercolor')

    if request.method == 'POST':
        item_form = OrderItemForm(request.POST)
        color_form = ColorSelectionForm(request.POST)
        if item_form.is_valid() and color_form.is_valid():
            with transaction.atomic():
                # دریافت محصول از فرم‌ معتبر
                product = item_form.cleaned_data['product']
                order_item = item_form.save(commit=False)
                order_item.order = order
                order_item.product = product          # ← حالا product تعریف شده است
                order_item.unit_price = product.base_price
                order_item.save()

                for part_value, _ in Color.PART_CHOICES:
                    code = color_form.cleaned_data.get(f'color_{part_value}')
                    if code:
                        Color.objects.create(
                            part=part_value,
                            code=code,
                            orderitem=order_item
                        )
                messages.success(request, f"آیتم '{order_item.product.name}' به سفارش اضافه شد.")
                if 'add_another' in request.POST:
                    return redirect('create_order_step2', order_id=order.id)
                else:
                    return redirect('order_list')
        else:
            for field, errors in item_form.errors.items():
                for error in errors:
                    messages.error(request, f"خطا در {field}: {error}")
            for field, errors in color_form.errors.items():
                for error in errors:
                    messages.error(request, f"خطا در رنگ‌ها: {error}")
    else:
        item_form = OrderItemForm()
        color_form = ColorSelectionForm()

    context = {
        'order': order,
        'item_form': item_form,
        'color_form': color_form,
        'existing_items': existing_items,
    }
    return render(request, 'orders/create_step2.html', context)


@login_required
def ajax_load_products(request):
    """بارگذاری محصولات بر اساس دسته برای فیلترهای وابسته"""
    category_id = request.GET.get('category')
    if category_id:
        products = Product.objects.filter(category_id=category_id).order_by('name')
    else:
        products = Product.objects.none()
    data = [{'id': p.id, 'name': str(p)} for p in products]
    return JsonResponse(data, safe=False)


@login_required
@staff_or_representative_required
def order_detail(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related(
            'items__product__category',
            'items__ordercolor',
            'items__logs'   # اضافه کردن prefetch برای logs
        ),
        id=order_id
    )
    
    # محاسبه وضعیت مراحل برای هر آیتم
    for item in order.items.all():
        log_stages = set(item.logs.values_list('stage', flat=True))
        item.stages = {stage: (stage in log_stages) for stage, _ in STATION_CHOICES}
    
    context = {
        'order': order,
        'station_choices': STATION_CHOICES,
        'has_any_tasks': order.tasks.exists(),
        'has_paint_tasks': order.tasks.filter(station_name='paint').exists(),
        'colspan': 10 + len(STATION_CHOICES),
    }
    return render(request, 'orders/order_detail.html', context)




def ajax_load_product_colors(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    raw_value = product.default_colors

    # ۱. اگر خودش دیکشنری است
    if isinstance(raw_value, dict):
        defaults = raw_value

    # ۲. اگر رشته است
    elif isinstance(raw_value, str):
        # ابتدا تلاش با json.loads (برای رشته‌های معتبر JSON)
        try:
            defaults = json.loads(raw_value)
        except json.JSONDecodeError:
            # اگر ناموفق بود، با ast.literal_eval تلاش کن (برای رشته‌های شبیه پایتون)
            try:
                defaults = ast.literal_eval(raw_value)
            except (ValueError, SyntaxError):
                logger.warning(f"Cannot parse default_colors for product {product_id}: {raw_value}")
                defaults = {}

        # اطمینان از اینکه نتیجه یک دیکشنری باشد
        if not isinstance(defaults, dict):
            defaults = {}
    else:
        defaults = {}

    return JsonResponse({'defaults': defaults})





# -------------------------------------------------------------------
#     ویراش  محصولات 
# -------------------------------------------------------------------

@login_required
@admin_or_manager_required
def product_bom_edit(request, product_id):
    product = get_object_or_404(Product, pk=product_id)

    # قطعاتی که در BOM این محصول استفاده شده‌اند
    part_qs = Part.objects.filter(productbom__product=product).distinct()

    PartFormSet = modelformset_factory(
        Part,
        form=PartForm,
        extra=0,
        can_delete=False
    )

    BOMFormSet = inlineformset_factory(
        Product,
        ProductBOM,
        fields=[
            'part', 'quantity', 'color_part',
            'allow_material_override', 'color_material_map',
            'size_affected', 'size_adjustment_rule',
        ],
        widgets={
            'color_material_map': forms.Textarea(attrs={'rows': 2, 'class': 'form-control form-control-sm'}),
            'size_adjustment_rule': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
        },
        extra=0,               # ← دیگر ردیف خالی اضافه نمی‌شود
        can_delete=True,
    )

    if request.method == 'POST':
        part_formset = PartFormSet(request.POST, prefix='parts', queryset=part_qs)
        bom_formset = BOMFormSet(request.POST, prefix='bom', instance=product)

        if part_formset.is_valid() and bom_formset.is_valid():
            part_formset.save()
            bom_formset.save()
            messages.success(request, '✅ اطلاعات با موفقیت ذخیره شد.')
            return redirect('product_bom_edit', product_id=product.id)
        else:
            # نمایش خطاها برای عیب‌یابی
            messages.error(request, '⚠️ خطا در ذخیره‌سازی. لطفاً فیلدها را بررسی کنید.')
            print("Part formset errors:", part_formset.errors)
            print("BOM formset errors:", bom_formset.errors)
    else:
        part_formset = PartFormSet(prefix='parts', queryset=part_qs)
        bom_formset = BOMFormSet(prefix='bom', instance=product)

    context = {
        'product': product,
        'part_formset': part_formset,
        'bom_formset': bom_formset,
    }
    return render(request, 'product_bom_edit.html', context)



@login_required
@admin_or_manager_required
def admin_product_list(request):
    products = Product.objects.select_related('category').all()
    categories = ProductCategory.objects.all()

    # جستجو
    q = request.GET.get('q')
    if q:
        products = products.filter(name__icontains=q)

    # فیلتر دسته‌بندی
    category_id = request.GET.get('category')
    if category_id:
        products = products.filter(category_id=category_id)

    context = {
        'products': products,
        'categories': categories,
        'selected_category': category_id,
        'search_query': q,
    }
    return render(request, 'admin_product_list.html', context)


# -------------------------------------------------------------------
#      کاتالوگ محصولات (لیست قیمت) — نمایش عکس، قیمت و لیست قطعات
# -------------------------------------------------------------------
@login_required
@admin_or_manager_required
def product_catalog(request):
    """کاتالوگ محصولات به سبک بروشور — هر دسته‌بندی با بنر معرفی و گرید محصولات."""
    from .utils import _parse_default_colors

    COLOR_SWATCH_MAP = {
        '1': '#efe9df', '2': '#ded2ba', '3': '#c9b896', '4': '#b89968',
        '5': '#9c7b4f', '6': '#7a5c3a', '7': '#5c4530', '8': '#3f3226',
        '9': '#2b2119', '10': '#1a1512',
        'جناغی': '#8a6a45', 'بتنی': '#9c9c94',
    }

    products_qs = Product.objects.select_related('category').prefetch_related(
        'bom__part__material'
    )

    q = request.GET.get('q')
    if q:
        products_qs = products_qs.filter(
            Q(name__icontains=q) | Q(category__name__icontains=q)
        )

    category_id = request.GET.get('category')
    if category_id:
        products_qs = products_qs.filter(category_id=category_id)

    products = list(products_qs.order_by('name', 'category__name'))

    for product in products:
        product.display_code = f"P-{product.id:04d}"
        first_bom = product.bom.first()
        product.display_material = (
            first_bom.part.material.name
            if first_bom and first_bom.part and first_bom.part.material
            else None
        )
        colors = _parse_default_colors(product)
        seen = []
        for code in colors.values():
            code = str(code)
            if code and code != 'nan' and code not in seen:
                seen.append(code)
        product.display_colors = [
            {'code': c, 'hex': COLOR_SWATCH_MAP.get(c, '#cbbfa8')}
            for c in seen
        ]

    collections = {}
    order = []
    for product in products:
        key = product.name
        if key not in collections:
            collections[key] = {'name': product.name, 'products': []}
            order.append(key)
        collections[key]['products'].append(product)

    collection_list = [collections[k] for k in order]
    for idx, col in enumerate(collection_list, start=1):
        col['index'] = idx
        col['hero_image'] = next(
            (p.image for p in col['products'] if p.image),
            None,
        )
        col['category_names'] = sorted({
            p.category.name for p in col['products'] if p.category
        })

    context = {
        'collection_list': collection_list,
        'total_collections': len(collection_list),
        'categories': ProductCategory.objects.all(),
        'selected_category': category_id,
        'search_query': q,
        'today': jdatetime.date.today().strftime('%Y/%m/%d'),
        'total_products': len(products),
    }
    return render(request, 'product_catalog.html', context)


@login_required
@admin_or_manager_required
def product_catalog_pdf(request):
    """ذخیره / دانلود کاتالوگ محصولات به‌صورت PDF — همان طرح بروشور صفحه HTML."""
    from .utils import _parse_default_colors, get_color_hex_map
    from .pdf_utils import render_pdf

    color_hex_map = get_color_hex_map()

    q = request.GET.get('q')
    category_id = request.GET.get('category')

    products_qs = Product.objects.select_related('category').prefetch_related(
        'bom__part__material'
    )
    if q:
        products_qs = products_qs.filter(
            Q(name__icontains=q) | Q(category__name__icontains=q)
        )
    if category_id:
        products_qs = products_qs.filter(category_id=category_id)

    products = list(products_qs.order_by('name', 'category__name'))

    for product in products:
        product.display_code = f"P-{product.id:04d}"
        first_bom = product.bom.first()
        product.display_material = (
            first_bom.part.material.name
            if first_bom and first_bom.part and first_bom.part.material else None
        )
        colors = _parse_default_colors(product)
        seen_codes = []
        for code in colors.values():
            code = str(code)
            if code and code != 'nan' and code not in seen_codes:
                seen_codes.append(code)
        product.display_colors = [
            {'code': c, 'hex': color_hex_map.get(c, '#cbbfa8')}
            for c in seen_codes
        ]

    collections = {}
    order = []
    for product in products:
        key = product.name
        if key not in collections:
            collections[key] = {'name': product.name, 'products': []}
            order.append(key)
        collections[key]['products'].append(product)

    collection_list = [collections[k] for k in order]
    for idx, col in enumerate(collection_list, start=1):
        col['index'] = idx
        col['hero_image'] = next((p.image for p in col['products'] if p.image), None)
        col['category_names'] = sorted({p.category.name for p in col['products'] if p.category})

    return render_pdf(
        'product_catalog_pdf.html',
        {
            'collection_list': collection_list,
            'total_collections': len(collection_list),
            'total_products': len(products),
            'today': jdatetime.date.today().strftime('%Y/%m/%d'),
            'representative_name': request.user.get_full_name() or request.user.username,
        },
        filename='catalog.pdf',
    )






# -------------------------------------------------------------------
#      خروجی اکسل از دیتا بیس (به‌روز شده با مدل‌های جدید)
# -------------------------------------------------------------------

MODEL_ORDER = [
    'ProductCategory',
    'Material',
    'Customer',
    'WorkerProfile',
    'Product',
    'Part',
    'ProductBOM',
    'Order',
    'OrderItem',
    'Color',
    'ProductionTask',
    'ProductionLog',
    'ProductionEvent',
    'PackagingUnit',
]

def get_model_by_name(name):
    for model in apps.get_app_config('product').get_models():
        if model.__name__ == name:
            return model
    return None


def _gregorian_to_shamsi_str(g_date):
    """تبدیل یک datetime.date میلادی (یا jdatetime.date) به رشته YYYY-MM-DD شمسی"""
    if isinstance(g_date, jdatetime.date):
        return g_date.strftime('%Y-%m-%d')
    if hasattr(g_date, 'year'):
        try:
            return jdatetime.date.fromgregorian(date=g_date).strftime('%Y-%m-%d')
        except:
            return ''
    return str(g_date)


def convert_dates_for_import(model, data):
    for field in model._meta.get_fields():
        if isinstance(field, (models.DateField, models.DateTimeField)):
            col = field.name
            if col not in data:
                continue

            val = data[col]
            is_datetime = isinstance(field, models.DateTimeField)

            # ۱. None/NaN → تاریخ امروز
            if val is None or (isinstance(val, float) and math.isnan(val)):
                if is_datetime:
                    data[col] = dt.datetime.now()
                else:
                    data[col] = jdatetime.date.today()
                continue

            # ۲. رشته
            if isinstance(val, str):
                stripped = val.strip()
                if stripped:
                    try:
                        if is_datetime:
                            # فرض می‌کنیم رشته‌ها میلادی هستند (مثل خروجی قبلی)
                            # در غیر این صورت می‌توانید _parse_shamsi_date و سپس تبدیل کنید
                            data[col] = dt.datetime.fromisoformat(stripped)
                        else:
                            # رشته شمسی → jdatetime.date
                            data[col] = _parse_shamsi_date(stripped)
                    except Exception:
                        data[col] = dt.datetime.now() if is_datetime else jdatetime.date.today()
                else:
                    data[col] = dt.datetime.now() if is_datetime else jdatetime.date.today()
                continue

            # ۳. عدد (سریال تاریخ اکسل)
            if isinstance(val, (int, float)):
                try:
                    base = dt.datetime(1899, 12, 30)
                    delta = dt.timedelta(days=int(val))
                    if isinstance(val, float) and val % 1 != 0:
                        delta += dt.timedelta(seconds=round((val % 1) * 86400))
                    greg = base + delta
                    if is_datetime:
                        data[col] = greg   # مستقیم datetime میلادی
                    else:
                        # تبدیل به jdatetime.date (برای PersianDateField)
                        data[col] = jdatetime.date.fromgregorian(date=greg.date())
                except Exception:
                    data[col] = dt.datetime.now() if is_datetime else jdatetime.date.today()
                continue

            # ۴. اشیاء Python date/datetime (ممکن است میلادی باشند)
            if isinstance(val, dt.date):
                try:
                    if isinstance(val, dt.datetime):
                        data[col] = val if is_datetime else jdatetime.date.fromgregorian(date=val.date())
                    else:  # فقط date
                        if is_datetime:
                            data[col] = dt.datetime.combine(val, dt.time.min)
                        else:
                            data[col] = jdatetime.date.fromgregorian(date=val)
                except Exception:
                    data[col] = dt.datetime.now() if is_datetime else jdatetime.date.today()
                continue

            # ۵. نوع غیرمنتظره → تاریخ امروز
            data[col] = dt.datetime.now() if is_datetime else jdatetime.date.today()
    return data


def convert_dates_for_export(model, df):
    """تاریخ‌های میلادی (DateField و DateTimeField) را به رشتهٔ شمسی مناسب تبدیل می‌کند"""
    for field in model._meta.get_fields():
        if isinstance(field, (models.DateField, models.DateTimeField)):
            col = field.name
            if col in df.columns:
                is_datetime = isinstance(field, models.DateTimeField)
                df[col] = df[col].apply(
                    lambda d: _gregorian_to_shamsi_str(d, is_datetime=is_datetime) if pd.notnull(d) else ''
                )
    return df


def _gregorian_to_shamsi_str(g_date, is_datetime=False):
    if isinstance(g_date, jdatetime.datetime):
        g_date = g_date.togregorian()
    if isinstance(g_date, jdatetime.date):
        g_date = g_date.togregorian()
    if isinstance(g_date, dt.datetime):
        return g_date.strftime('%Y-%m-%d %H:%M:%S') if is_datetime else g_date.strftime('%Y-%m-%d')
    if isinstance(g_date, dt.date):
        return g_date.strftime('%Y-%m-%d')
    return str(g_date)


def _parse_shamsi_date(date_str):
    """تبدیل رشته شمسی (YYYY-MM-DD یا YYYY/MM/DD) به jdatetime.date"""
    parts = re.split(r'[-/]', date_str.strip())
    if len(parts) == 3:
        try:
            y, m, d = map(int, parts)
            return jdatetime.date(y, m, d)
        except (ValueError, TypeError):
            pass
    # اگر نامعتبر بود، تاریخ امروز را برگردان
    return jdatetime.date.today()


def clean_data_for_model(model, data):
    NA_STRINGS = {'nan', 'none', 'null', 'na', ''}
    for field in model._meta.get_fields():
        if not field.is_relation and hasattr(field, 'null'):
            field_name = field.name
            if field_name not in data:
                continue
            value = data[field_name]

            # تبدیل float به int اگر عدد صحیح است
            if isinstance(value, float) and not math.isnan(value) and value == int(value):
                value = int(value)

            # تبدیل رشته‌های 'nan'/'none' به None
            if isinstance(value, str) and value.strip().lower() in NA_STRINGS:
                value = None
            elif isinstance(value, float) and math.isnan(value):
                value = None

            # **تاریخ و تاریخ-زمان: بعد از convert_dates_for_import، دیگر کاری نکنیم**
            if isinstance(field, (models.DateField, models.DateTimeField)):
                continue

            if value is None:
                if isinstance(field, (models.CharField, models.TextField)):
                    data[field_name] = ''
                elif isinstance(field, (models.IntegerField, models.DecimalField, models.FloatField)):
                    if field.has_default():
                        del data[field_name]
                    else:
                        data[field_name] = 0
                elif isinstance(field, models.JSONField):
                    data[field_name] = {}
                elif isinstance(field, models.BooleanField):
                    data[field_name] = False
                else:
                    if not field.null:
                        del data[field_name]
            else:
                if isinstance(field, models.CharField) and not isinstance(value, str):
                    data[field_name] = str(value)


def resolve_foreign_keys(model, data):
    for field in model._meta.get_fields():
        if field.is_relation and field.many_to_one:
            fk_attname = field.attname
            if fk_attname in data:
                fk_value = data[fk_attname]
                if fk_value is not None and not (isinstance(fk_value, float) and math.isnan(fk_value)):
                    related_model = field.related_model
                    try:
                        obj = related_model.objects.get(pk=int(fk_value))
                        data[field.name] = obj
                    except (related_model.DoesNotExist, ValueError, TypeError):
                        pass
                del data[fk_attname]




# -------------------------------------------------------------------
# ویوهای Export و Import
# -------------------------------------------------------------------
@login_required
@admin_or_manager_required
def export_all_data(request):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for model_name in MODEL_ORDER:
            model = get_model_by_name(model_name)
            if not model:
                continue
            queryset = model.objects.all()
            df = pd.DataFrame(list(queryset.values()))
            # حذف ستون‌های غیر ضروری
            for col in ['qr_code', 'image', 'password']:
                if col in df.columns:
                    df.drop(col, axis=1, inplace=True)
            # تبدیل تاریخ‌ها به شمسی
            df = convert_dates_for_export(model, df)
            df.to_excel(writer, sheet_name=model_name, index=False)

    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="selvi_all_data.xlsx"'
    return response

@login_required
@admin_or_manager_required
def import_data(request):
    if request.method == 'POST':
        file = request.FILES.get('excel_file')
        if not file:
            messages.error(request, 'فایلی انتخاب نشده است.')
            return redirect('import_data')

        try:
            xls = pd.ExcelFile(file)
            sheets_processed = 0
            for sheet_name in MODEL_ORDER:
                if sheet_name not in xls.sheet_names:
                    continue
                model = get_model_by_name(sheet_name)
                if not model:
                    continue

                df = pd.read_excel(file, sheet_name=sheet_name)
                df = df.where(pd.notnull(df), None)

                for _, row in df.iterrows():
                    data = row.to_dict()
                    data.pop('qr_code', None)
                    data.pop('image', None)

                    raw_id = data.pop('id', None)

                    # ۱. تبدیل تاریخ‌های شمسی به jdatetime
                    data = convert_dates_for_import(model, data)
                    # ۲. تمیزسازی عمومی
                    clean_data_for_model(model, data)
                    # ۳. کلیدهای خارجی
                    resolve_foreign_keys(model, data)

                    valid_id = None
                    if raw_id is not None:
                        if isinstance(raw_id, (int, float)):
                            if not math.isnan(raw_id):
                                valid_id = int(raw_id)
                        elif isinstance(raw_id, str):
                            raw_id = raw_id.strip()
                            if raw_id.lower() not in {'nan', 'none', 'null', ''}:
                                try:
                                    valid_id = int(float(raw_id))
                                except ValueError:
                                    pass

                    if valid_id is not None:
                        model.objects.update_or_create(id=valid_id, defaults=data)
                    else:
                        model.objects.create(**data)

                sheets_processed += 1

            messages.success(request, f'{sheets_processed} جدول با موفقیت پردازش شدند.')
        except Exception as e:
            import traceback
            logger.error("Import failed with traceback:")
            logger.error(traceback.format_exc())
            messages.error(request, f'خطا در پردازش فایل: {e}')

        return redirect('import_data')

    return render(request, 'import_data.html')






# -------------------------------------------------------------------
# اسکن بسته بندی
# -------------------------------------------------------------------

# views.py (بخش مربوط به scan_packaging_unit – جایگزین کامل)

@login_required
def scan_packaging_unit(request, pk):
    from .decorators import is_warehouse_user
    from .utils import _parse_default_colors as _parse_default_colors_for_view
    unit = get_object_or_404(PackagingUnit, pk=pk)
    worker_stage = request.user.workerprofile.stage if hasattr(request.user, 'workerprofile') else None
    item = unit.order_item
    next_url = request.GET.get('next', 'dashboard')

    # ---------- شاخه ۱: مونتاژ -> ثبت خرابی با انتخاب بخش رنگی ----------
    if worker_stage in ('mon', 'assembly2'):
        available_parts = list(item.ordercolor.values_list('part', flat=True))
        if not available_parts:
            available_parts = list(_parse_default_colors_for_view(item.product).keys())
        label_map = dict(Color.PART_CHOICES)

        if request.method == 'POST':
            color_part = request.POST.get('color_part', '').strip()
            description = request.POST.get('description', '').strip() or 'خرابی ثبت‌شده هنگام اسکن بسته‌بندی'
            if not color_part or color_part not in available_parts:
                messages.error(request, 'بخش رنگی خراب را انتخاب کنید.')
            else:
                related_task = ProductionTask.objects.filter(
                    order_item=item, station_name='paint', color_part=color_part
                ).order_by('-step_order').first()
                with transaction.atomic():
                    existing = ProductionDefect.objects.filter(
                        packaging_unit=unit,
                        color_part=color_part,
                        status__in=['reported', 'material_requested', 'rework_issued'],
                    ).select_for_update().first()
                    if existing:
                        messages.warning(
                            request,
                            f'برای این بخش قبلاً خرابی ثبت شده است (#{existing.id}).'
                        )
                        return redirect('item_detail', pk=item.id)
                    ProductionDefect.objects.create(
                        task=related_task, order=item.order, order_item=item,
                        packaging_unit=unit, color_part=color_part, quantity=1,
                        description=description, reported_by=request.user,
                    )
                messages.success(request, f'خرابی برای بخش «{label_map.get(color_part, color_part)}» ثبت شد.')
                return redirect('item_detail', pk=item.id)

        return render(request, 'scan_defect_report.html', {
            'unit': unit, 'item': item,
            'color_parts': [(p, label_map.get(p, p)) for p in available_parts],
        })

    if worker_stage == 'paint' and not request.user.groups.filter(name='انبار').exists():
        from inventory.models import MaterialIssue, RawMaterial

        reported_defects = list(
            ProductionDefect.objects.filter(
                packaging_unit=unit, status='reported'
            ).select_related(
                'task__painting_stage__process', 'order_item', 'order'
            ).order_by('created_at')
        )
        raw_materials = list(
            RawMaterial.objects.filter(is_active=True).order_by('category__name', 'name')
        )
        context = {
            'unit': unit,
            'item': unit.order_item,
            'defects': reported_defects,
            'raw_materials': raw_materials,
            'next_url': next_url,
        }

        if request.method == 'POST':
            defect_ids = []
            for value in request.POST.getlist('defect_ids'):
                try:
                    defect_id = int(value)
                except (TypeError, ValueError):
                    continue
                if defect_id not in defect_ids:
                    defect_ids.append(defect_id)
            if not defect_ids or not reported_defects:
                messages.error(request, 'خرابی ثبت‌شده‌ای برای درخواست مواد انتخاب نشده است.')
                return render(request, 'scan_material_request_for_defect.html', context)

            defects_by_id = {
                defect.id: defect
                for defect in ProductionDefect.objects.filter(
                    pk__in=defect_ids, packaging_unit=unit, status='reported'
                ).select_related(
                    'task__painting_stage__process', 'order_item', 'order'
                )
            }
            defects = [
                defects_by_id[defect_id]
                for defect_id in defect_ids
                if defect_id in defects_by_id
            ]
            if len(defects) != len(defect_ids):
                messages.error(request, 'یکی از خرابی‌های انتخاب‌شده معتبر نیست یا وضعیت آن تغییر کرده است.')
                return render(request, 'scan_material_request_for_defect.html', context)

            errors = []
            raw_ids_by_defect = {}
            quantities_by_defect = {}
            raw_ids = []
            for defect in defects:
                raw_id = request.POST.get(f'raw_material_{defect.id}', '').strip()
                qty_str = request.POST.get(f'quantity_{defect.id}', '').strip()
                if not raw_id or not qty_str:
                    errors.append(defect.id)
                    continue
                try:
                    raw_id_int = int(raw_id)
                except (TypeError, ValueError):
                    errors.append(defect.id)
                    continue
                try:
                    quantity = Decimal(qty_str)
                    if not quantity.is_finite() or quantity <= 0:
                        raise ValueError
                except (TypeError, ValueError, ArithmeticError):
                    errors.append(defect.id)
                    continue
                raw_ids_by_defect[str(defect.id)] = raw_id_int
                quantities_by_defect[str(defect.id)] = quantity
                raw_ids.append(raw_id_int)

            if errors:
                messages.error(request, 'برای هر خرابی انتخاب‌شده، ماده اولیه و مقدار معتبر وارد کنید.')
                return render(request, 'scan_material_request_for_defect.html', context)

            raw_materials_by_id = {
                str(material.id): material
                for material in RawMaterial.objects.filter(
                    pk__in=raw_ids, is_active=True
                )
            }
            if any(str(raw_id) not in raw_materials_by_id for raw_id in raw_ids):
                messages.error(request, 'ماده اولیه انتخاب‌شده معتبر یا فعال نیست.')
                return render(request, 'scan_material_request_for_defect.html', context)

            with transaction.atomic():
                locked_defects = {
                    str(defect.id): defect
                    for defect in ProductionDefect.objects.select_for_update().filter(
                        pk__in=defect_ids, packaging_unit=unit, status='reported'
                    )
                }
                duplicate_request = False
                for defect in defects:
                    locked_defect = locked_defects.get(str(defect.id))
                    if locked_defect is None:
                        messages.error(request, 'وضعیت یکی از خرابی‌ها تغییر کرده است؛ درخواست ثبت نشد.')
                        return render(request, 'scan_material_request_for_defect.html', context)
                    raw_material = raw_materials_by_id[str(raw_ids_by_defect[str(defect.id)])]
                    if MaterialIssue.objects.filter(
                        defect=locked_defect,
                        raw_material=raw_material,
                        purpose='rework',
                        status__in=['requested', 'partial'],
                    ).exists():
                        duplicate_request = True

                if duplicate_request:
                    messages.error(request, 'برای یکی از خرابی‌ها قبلاً درخواست مواد ثبت شده است.')
                    return render(request, 'scan_material_request_for_defect.html', context)

                for defect in defects:
                    locked_defect = locked_defects[str(defect.id)]
                    raw_material = raw_materials_by_id[str(raw_ids_by_defect[str(defect.id)])]
                    MaterialIssue.objects.create(
                        task=locked_defect.task,
                        defect=locked_defect,
                        packaging_unit=unit,
                        order_item=unit.order_item,
                        color_part=locked_defect.color_part,
                        raw_material=raw_material,
                        requested_quantity=quantities_by_defect[str(defect.id)],
                        purpose='rework',
                        status='requested',
                        requested_by=request.user,
                        note=f'درخواست جبران خرابی #{locked_defect.id} — {locked_defect.get_color_part_display()}',
                    )
                    locked_defect.status = 'material_requested'
                    locked_defect.save(update_fields=['status'])

            messages.success(request, 'درخواست مواد ثبت شد و به صف تحویل انبار ارسال شد.')
            return redirect('item_detail', pk=item.id)

        return render(request, 'scan_material_request_for_defect.html', context)

    # ---------- شاخه ۲: گروه «انبار» -> ثبت تحویل ماده اولیه ----------
    if is_warehouse_user(request.user):
        from inventory.models import RawMaterial, StockMovement
        pending_tasks = ProductionTask.objects.filter(
            order_item=item
        ).exclude(status='done').select_related('painting_stage', 'part').order_by('step_order')

        if request.method == 'POST':
            raw = get_object_or_404(RawMaterial, pk=request.POST.get('raw_material_id'))
            qty_str = request.POST.get('quantity', '0')
            task_id = request.POST.get('task_id') or None
            try:
                qty = Decimal(qty_str)
            except Exception:
                qty = Decimal('0')

            if qty <= 0:
                messages.error(request, 'مقدار تحویل باید بزرگ‌تر از صفر باشد.')
            else:
                StockMovement.objects.create(
                    raw_material=raw, movement_type='consumption', quantity=qty,
                    reference_task_id=task_id, created_by=request.user,
                    note=f'تحویل دستی هنگام اسکن بسته‌بندی — آیتم {item.id} (سفارش {item.order_id})',
                )
                messages.success(request, f'تحویل {qty} {raw.get_unit_display()} از «{raw.name}» ثبت شد.')
                return redirect('item_detail', pk=item.id)

        return render(request, 'scan_material_issue.html', {
            'unit': unit, 'item': item, 'tasks': pending_tasks,
            'raw_materials': RawMaterial.objects.filter(is_active=True).order_by('category__name', 'name'),
        })

    # ---------- شاخه سوم (پیش‌فرض/موجود): بسته‌بندی و ارسال ----------
    if request.method == 'POST':
        # --- بسته‌بندی ---
        if worker_stage == 'packaging':
            if not unit.is_packed:
                unit.is_packed = True
                unit.packed_at = timezone.now()
                unit.packed_by = request.user
                unit.save()

                item = unit.order_item
                if item.is_fully_packed and not ProductionLog.objects.filter(
                    order_item=item, stage='packaging'
                ).exists():
                    ProductionLog.objects.create(
                        order_item=item, stage='packaging', user=request.user,
                        notes='همه واحدها بسته‌بندی شدند'
                    )
                messages.success(request, f'✅ واحد {unit.unit_number} بسته‌بندی شد.')
            else:
                messages.warning(request, 'این واحد قبلاً بسته‌بندی شده است.')

        # --- ارسال ---
        elif worker_stage == 'shipping':
            if not unit.is_packed:
                messages.error(request, '⛔ این واحد هنوز بسته‌بندی نشده است. ابتدا باید بسته‌بندی شود.')
                return redirect(next_url)

            # دریافت پلاک از فرم (الزامی)
            plate = request.POST.get('plate', '').strip()
            if not plate:
                messages.error(request, 'لطفاً پلاک خودرو را وارد کنید.')
                # نمایش مجدد صفحه تأیید با خطا
                context = {
                    'unit': unit,
                    'can_pack': False,
                    'can_ship': True,
                    'next_url': next_url,
                    'saved_plate': request.session.get('current_plate', ''),
                }
                return render(request, 'scan_packaging_unit.html', context)

            # ذخیره در session برای استفاده‌های بعدی
            request.session['current_plate'] = plate

            if not unit.is_shipped:
                unit.is_shipped = True
                unit.shipped_at = timezone.now()
                unit.shipped_by = request.user
                unit.save()

                # ثبت لاگ محموله با پلاک صحیح
                ShipmentLog.objects.create(
                    packaging_unit=unit,
                    plate_number=plate,
                    shipped_by=request.user
                )

                item = unit.order_item
                if item.is_fully_shipped and not ProductionLog.objects.filter(
                    order_item=item, stage='shipping'
                ).exists():
                    ProductionLog.objects.create(
                        order_item=item, stage='shipping', user=request.user,
                        notes='همه واحدها ارسال شدند'
                    )
                messages.success(request, f'🚚 واحد {unit.unit_number} ارسال شد.')
            else:
                messages.warning(request, 'این واحد قبلاً ارسال شده است.')

        else:
            messages.error(request, 'شما دسترسی لازم برای این عملیات را ندارید.')

        return redirect(next_url)

    # ==================================================================
    # GET: نمایش صفحه تأیید
    # ==================================================================
    context = {
        'unit': unit,
        'can_pack': worker_stage == 'packaging' and not unit.is_packed,
        'can_ship': worker_stage == 'shipping' and unit.is_packed and not unit.is_shipped,
        'next_url': next_url,
        'saved_plate': request.session.get('current_plate', ''),  # پلاک قبلی (در صورت وجود)
    }
    return render(request, 'scan_packaging_unit.html', context)


@login_required
def undo_packaging_unit(request, pk):
    unit = get_object_or_404(PackagingUnit, pk=pk)
    worker_stage = request.user.workerprofile.stage if hasattr(request.user, 'workerprofile') else None

    if worker_stage not in ['packaging', 'shipping']:
        messages.error(request, 'شما دسترسی لازم برای لغو عملیات را ندارید.')
        return redirect('dashboard')

    # ---------- لغو بسته‌بندی ----------
    if worker_stage == 'packaging':
        if not unit.is_packed:
            messages.warning(request, 'این واحد هنوز بسته‌بندی نشده است.')
        elif unit.packed_by != request.user and not request.user.is_superuser:
            messages.error(request, 'فقط اپراتوری که این واحد را بسته‌بندی کرده می‌تواند آن را لغو کند.')
        else:
            unit.is_packed = False
            unit.packed_at = None
            unit.packed_by = None
            unit.save()

            # حذف ProductionLog مربوط به تکمیل بسته‌بندی (اگر وجود دارد)
            ProductionLog.objects.filter(order_item=unit.order_item, stage='packaging').delete()
            messages.success(request, f'✅ بسته‌بندی واحد {unit.unit_number} لغو شد.')

    # ---------- لغو ارسال ----------
    elif worker_stage == 'shipping':
        if not unit.is_shipped:
            messages.warning(request, 'این واحد هنوز ارسال نشده است.')
        elif unit.shipped_by != request.user and not request.user.is_superuser:
            messages.error(request, 'فقط اپراتوری که این واحد را ارسال کرده می‌تواند آن را لغو کند.')
        else:
            unit.is_shipped = False
            unit.shipped_at = None
            unit.shipped_by = None
            unit.save()

            # حذف ShipmentLog مربوط به این واحد (آخرین لاگ ارسال)
            ShipmentLog.objects.filter(packaging_unit=unit).delete()
            # حذف ProductionLog مربوط به تکمیل ارسال (اگر وجود دارد)
            ProductionLog.objects.filter(order_item=unit.order_item, stage='shipping').delete()
            messages.success(request, f'🚚 ارسال واحد {unit.unit_number} لغو شد.')

    next_url = request.GET.get('next', 'dashboard')
    return redirect(next_url)



# -------------------------------------------------------------------
# ۱. داشبورد مشتری (لیست سفارش‌ها)
# -------------------------------------------------------------------

@login_required
def customer_order_list(request):
    # نمایش تمام سفارش‌هایی که این کاربر ثبت کرده است (مستقل از Customer)
    orders = Order.objects.filter(user=request.user).order_by('-id')
    return render(request, 'customer/order_list.html', {'orders': orders})



# -------------------------------------------------------------------
# ۲. مرحلهٔ اول – اطلاعات مشتری
# -------------------------------------------------------------------

@login_required
def customer_create_order(request):
    existing_customer = Customer.objects.filter(user=request.user).first()

    if request.method == 'POST':
        form = CustomerInfoForm(request.POST)
        item_form = OrderItemForm(request.POST)
        color_form = ColorSelectionForm(request.POST)
        if form.is_valid() and item_form.is_valid() and color_form.is_valid():
            with transaction.atomic():
                name = form.cleaned_data['name']
                phone = form.cleaned_data.get('phone', '')
                address = form.cleaned_data.get('address', '')
                number = form.cleaned_data.get('number', '')

                customer = Customer.objects.create(
                    user=request.user,
                    name=name,
                    phone=phone,
                    address=address
                )

                order = Order.objects.create(
                    user=request.user,
                    customer=customer,
                    number=number,
                    status='draft'
                )

                product = item_form.cleaned_data['product']
                order_item = item_form.save(commit=False)
                order_item.order = order
                order_item.product = product
                order_item.unit_price = product.base_price
                order_item.save()

                for part_value, _ in Color.PART_CHOICES:
                    code = color_form.cleaned_data.get(f'color_{part_value}')
                    if code:
                        Color.objects.create(part=part_value, code=code, orderitem=order_item)

            messages.success(request, 'سفارش جدید ایجاد شد.')
            if 'add_another' in request.POST:
                return redirect('customer_order_detail', order_id=order.id)
            return redirect('customer_order_detail', order_id=order.id)
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"خطا در {field}: {error}")
            for field, errors in item_form.errors.items():
                for error in errors:
                    messages.error(request, f"خطا در {field}: {error}")
            for field, errors in color_form.errors.items():
                for error in errors:
                    messages.error(request, f"خطا در رنگ‌ها: {error}")
    else:
        initial = {}
        if existing_customer:
            initial = {
                'name': existing_customer.name,
                'phone': existing_customer.phone,
                'address': existing_customer.address,
            }
        form = CustomerInfoForm(initial=initial)
        item_form = OrderItemForm()
        color_form = ColorSelectionForm()

    context = {
        'form': form,
        'item_form': item_form,
        'color_form': color_form,
    }
    return render(request, 'customer/create_order.html', context)




# -------------------------------------------------------------------
# ۳. مرحلهٔ دوم – افزودن محصولات
# -------------------------------------------------------------------
# -------------------------------------------------------------------
#    پیش فاکتور  
# -------------------------------------------------------------------
@login_required
def order_invoice(request, order_id):
    order = get_object_or_404(Order.objects.prefetch_related('items__product__category', 'items__ordercolor'), id=order_id)
    if not (request.user.is_superuser or order.user == request.user):
        return HttpResponseForbidden()
    return render(request, 'order_invoice.html', {'order': order})






# -------------------------------------------------------------------
#    ویرایش سفارش مشتری
# -------------------------------------------------------------------
@login_required
def customer_edit_order_item(request, item_id):
    item = get_object_or_404(OrderItem, pk=item_id)

    if item.order.user != request.user:
        messages.error(request, "شما اجازه ویرایش این آیتم را ندارید.")
        return redirect('customer_order_list')

    if not (item.order.status == 'draft' and item.order.created_at == jdatetime.date.today()):
        messages.error(request, "فقط سفارش‌های پیش‌نویس امروز قابل ویرایش هستند.")
        return redirect('customer_order_detail', order_id=item.order.id)

    existing_colors = {c.part: c.code for c in item.ordercolor.all()}

    if request.method == 'POST':
        item_form = EditOrderItemForm(request.POST, instance=item)
        color_form = ColorSelectionForm(request.POST)

        if item_form.is_valid() and color_form.is_valid():
            with transaction.atomic():
                new_product = item_form.cleaned_data['product']
                if item.product != new_product:
                    item.product = new_product
                    item.unit_price = new_product.base_price
                item_form.save()

                # به‌روزرسانی رنگ‌ها
                item.ordercolor.all().delete()
                for part_value, _ in Color.PART_CHOICES:
                    code = color_form.cleaned_data.get(f'color_{part_value}')
                    if code:
                        Color.objects.create(part=part_value, code=code, orderitem=item)

                messages.success(request, "آیتم با موفقیت ویرایش شد.")
                return redirect('customer_order_detail', order_id=item.order.id)
    else:
        item_form = EditOrderItemForm(instance=item)
        color_form = ColorSelectionForm(initial={
            f'color_{part}': existing_colors.get(part, '')
            for part, _ in Color.PART_CHOICES
        })

    return render(request, 'customer/edit_order_item.html', {
        'item_form': item_form,
        'color_form': color_form,
        'item': item,
    })



# -------------------------------------------------------------------
# مشتری: جزئیات سفارش
# -------------------------------------------------------------------
@login_required
def customer_order_detail(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    if order.user != request.user:
        messages.error(request, "شما اجازه مشاهده این سفارش را ندارید.")
        return redirect('customer_order_list')

    can_edit = (order.status == 'draft' and order.created_at == jdatetime.date.today())
    
    # فرم افزودن آیتم جدید
    add_item_form = OrderItemForm()
    add_color_form = ColorSelectionForm()

    context = {
        'order': order,
        'can_edit': can_edit,
        'add_item_form': add_item_form,
        'add_color_form': add_color_form,
        'edit_customer_form': CustomerInfoForm(initial={
            'name': order.customer.name,
            'phone': order.customer.phone,
            'address': order.customer.address,
            'number': order.number,
        }),
    }
    return render(request, 'customer/order_detail.html', context)




# -------------------------------------------------------------------
# مشتری: ویرایش اطلاعات کلی سفارش (نام، شماره)
# -------------------------------------------------------------------
@login_required
def customer_edit_order_info(request, order_id):
    order = get_object_or_404(Order, pk=order_id, user=request.user)
    if not (order.status == 'draft' and order.created_at == jdatetime.date.today()):
        messages.error(request, "فقط سفارش‌های پیش‌نویس امروز قابل ویرایش هستند.")
        return redirect('customer_order_detail', order_id=order.id)

    if request.method == 'POST':
        form = CustomerInfoForm(request.POST)
        if form.is_valid():
            # به‌روزرسانی Customer
            customer = order.customer
            customer.name = form.cleaned_data['name']
            customer.phone = form.cleaned_data.get('phone', '')
            customer.address = form.cleaned_data.get('address', '')
            customer.save()

            # به‌روزرسانی شماره سفارش
            order.number = form.cleaned_data.get('number', '')
            order.save()

            messages.success(request, "اطلاعات سفارش به‌روز شد.")
            return redirect('customer_order_detail', order_id=order.id)

    return redirect('customer_order_detail', order_id=order.id)


# -------------------------------------------------------------------
# مشتری: افزودن آیتم جدید به سفارش
# -------------------------------------------------------------------
@login_required
def customer_add_item(request, order_id):
    order = get_object_or_404(Order, pk=order_id, user=request.user)
    if not (order.status == 'draft' and order.created_at == jdatetime.date.today()):
        messages.error(request, "فقط سفارش‌های پیش‌نویس امروز قابل ویرایش هستند.")
        return redirect('customer_order_detail', order_id=order.id)

    if request.method == 'POST':
        item_form = OrderItemForm(request.POST)
        color_form = ColorSelectionForm(request.POST)
        if item_form.is_valid() and color_form.is_valid():
            with transaction.atomic():
                product = item_form.cleaned_data['product']
                order_item = item_form.save(commit=False)
                order_item.order = order
                order_item.product = product
                order_item.unit_price = product.base_price
                order_item.save()

                for part_value, _ in Color.PART_CHOICES:
                    code = color_form.cleaned_data.get(f'color_{part_value}')
                    if code:
                        Color.objects.create(part=part_value, code=code, orderitem=order_item)

                messages.success(request, f'{product.name} به سفارش اضافه شد.')
                return redirect('customer_order_detail', order_id=order.id)
        else:
            messages.error(request, 'لطفاً خطاهای فرم را بررسی کنید.')
    return redirect('customer_order_detail', order_id=order.id)


# -------------------------------------------------------------------
# مشتری: ویرایش یک آیتم
# -------------------------------------------------------------------
@login_required
def customer_edit_order_item(request, item_id):
    item = get_object_or_404(OrderItem, pk=item_id)
    if item.order.user != request.user:
        messages.error(request, "شما اجازه ویرایش این آیتم را ندارید.")
        return redirect('customer_order_list')
    if not (item.order.status == 'draft' and item.order.created_at == jdatetime.date.today()):
        messages.error(request, "فقط سفارش‌های پیش‌نویس امروز قابل ویرایش هستند.")
        return redirect('customer_order_detail', order_id=item.order.id)

    # رنگ‌های فعلی
    existing_colors = {c.part: c.code for c in item.ordercolor.all()}

    if request.method == 'POST':
        item_form = EditOrderItemForm(request.POST, instance=item)
        color_form = ColorSelectionForm(request.POST)
        if item_form.is_valid() and color_form.is_valid():
            with transaction.atomic():
                item_form.save()
                # حذف رنگ‌های قبلی و ایجاد جدید
                item.ordercolor.all().delete()
                for part_value, _ in Color.PART_CHOICES:
                    code = color_form.cleaned_data.get(f'color_{part_value}')
                    if code:
                        Color.objects.create(part=part_value, code=code, orderitem=item)
                messages.success(request, "آیتم ویرایش شد.")
                return redirect('customer_order_detail', order_id=item.order.id)
    else:
        item_form = EditOrderItemForm(
            instance=item,
            initial={
                'category': item.product.category.id,
                'product': item.product.id,
            }
        )
        item_form.fields['product'].widget.attrs['data-initial-product'] = item.product.id
        color_form = ColorSelectionForm(initial={
            f'color_{part}': existing_colors.get(part, '')
            for part, _ in Color.PART_CHOICES
        })

    return render(request, 'customer/edit_order_item.html', {
        'item_form': item_form,
        'color_form': color_form,
        'item': item,
    })


# -------------------------------------------------------------------
# مشتری: حذف یک آیتم سفارش
# -------------------------------------------------------------------
@login_required
def customer_delete_order_item(request, item_id):
    item = get_object_or_404(OrderItem, pk=item_id)

    # فقط صاحب سفارش بتواند حذف کند
    if item.order.user != request.user:
        messages.error(request, "شما اجازه حذف این آیتم را ندارید.")
        return redirect('customer_order_list')

    # فقط پیش‌نویس‌های امروز
    if item.order.status != 'draft' or item.order.created_at != jdatetime.date.today():
        messages.error(request, "فقط سفارش‌های پیش‌نویس امروز قابل حذف هستند.")
        return redirect('customer_order_list')

    order_id = item.order.id
    item_name = item.product.name
    item.delete()
    messages.success(request, f"آیتم «{item_name}» با موفقیت حذف شد.")
    return redirect('customer_order_detail', order_id=order_id)



# -------------------------------------------------------------------
#    پرینت همه برگه های سفارش
# -------------------------------------------------------------------
@login_required
def order_combined_print(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related(
            'items__product__category',
            'items__product__bom__part',
            'items__ordercolor',
            'items__packaging_units',
        ),
        id=order_id
    )

    # آماده‌سازی داده‌ها برای هر آیتم
    items_data = []
    for item in order.items.all():
        # رنگ
        item.color = item.color_summary

        # BOM
        bom_list = item.product.bom.all()
        item.bompart = [{'part': b.part, 'quantity': b.quantity} for b in bom_list]

        # واحدهای بسته‌بندی
        units = item.packaging_units.all()

        items_data.append({
            'item': item,
            'units': units,
        })

    context = {
        'order': order,
        'items_data': items_data,
    }
    return render(request, 'order_combined_print.html', context)




from .models import PackagingUnit, ShipmentLog  
# -------------------------------------------------------------------
#   برگه خروج  بارگیری  
# -------------------------------------------------------------------
@login_required
@staff_or_representative_required
def report_shipped(request):
    # ---------- تاریخ شمسی (پیش‌فرض امروز) ----------
    date_str = request.GET.get('date')
    if date_str:
        try:
            y, m, d = map(int, date_str.split('-'))
            persian_date = jdatetime.date(y, m, d)
        except (ValueError, TypeError):
            persian_date = jdatetime.date.today()
    else:
        persian_date = jdatetime.date.today()

    gregorian_date = persian_date.togregorian()

    # ---------- کوئری پایه ----------
    units = PackagingUnit.objects.filter(
        is_shipped=True,
        shipped_at__date=gregorian_date
    ).select_related(
        'order_item__order__customer',
        'order_item__order__user',
        'order_item__product__category',
        'shipped_by'
    ).prefetch_related(
        'order_item__ordercolor',
        'shipment_logs'            # ← برای فیلتر پلاک لازم است
    )
    units = units.order_by('order_item__product__category__name', 'order_item__product__name')
    # ---------- فیلتر پلاک ----------
    plate = request.GET.get('plate')
    if plate:
        units = units.filter(shipment_logs__plate_number=plate)

    # ---------- فیلتر نماینده ----------
    representative_id = request.GET.get('representative')
    if representative_id:
        units = units.filter(order_item__order__user_id=representative_id)

    # ---------- فیلتر شماره سفارش ----------
    order_id = request.GET.get('order_id')
    if order_id:
        units = units.filter(order_item__order_id=order_id)

    # ---------- محاسبات مشترک ----------
    representative_name = ""
    total_price = 0
    if units.exists():
        if representative_id:
            try:
                rep = User.objects.get(pk=representative_id)
                representative_name = rep.get_full_name() or rep.username
            except User.DoesNotExist:
                pass
        else:
            representative_name = units.first().order_item.order.user.get_full_name() or units.first().order_item.order.user.username
        total_price = sum(unit.order_item.unit_price for unit in units)

    # ---------- درخواست چاپ برگه ترخیص ----------
    if request.GET.get('print'):
        return render(request, 'reports/delivery_note.html', {
            'units': units,
            'today': persian_date,
            'representative_name': representative_name,
            'total_price': total_price,
            'plate': request.GET.get('plate', ''),   # ← پلاک از فیلتر

        })

    # ---------- لیست نمایندگان برای dropdown ----------
    representatives = User.objects.filter(
        order__items__packaging_units__is_shipped=True
    ).distinct().order_by('username')

    # ---------- لیست پلاک‌های موجود (واقعی) ----------
    plates = ShipmentLog.objects.values_list('plate_number', flat=True).distinct().order_by('plate_number')

    context = {
        'units': units,
        'representatives': representatives,
        'selected_representative': representative_id,
        'selected_date': persian_date.strftime('%Y-%m-%d'),
        'selected_order': order_id or '',
        'representative_name': representative_name,
        'total_price': total_price,
        'plates': plates,
        'selected_plate': plate or '',
    }
    return render(request, 'reports/shipped.html', context)


@login_required
@staff_or_representative_required
def report_ready_to_ship(request):
    representative_id = request.GET.get('representative')

    units = PackagingUnit.objects.filter(
        is_packed=True,
        is_shipped=False,
    ).select_related(
        'order_item__order__customer',
        'order_item__order__user',
        'order_item__product__category',
    ).prefetch_related('order_item__ordercolor').order_by(
        'order_item__order__user__username', 'order_item__order_id', 'order_item__product__name'
    )

    if representative_id:
        units = units.filter(order_item__order__user_id=representative_id)

    representatives = User.objects.filter(
        order__items__packaging_units__is_packed=True,
        order__items__packaging_units__is_shipped=False,
    ).distinct().order_by('username')

    representative_name = 'همه نمایندگان'
    if representative_id:
        rep = User.objects.filter(pk=representative_id).first()
        if rep:
            representative_name = rep.get_full_name() or rep.username

    if request.GET.get('print'):
        return render(request, 'reports/ready_to_ship_print.html', {
            'units': units,
            'today': dt.date.today(),
            'representative_name': representative_name,
        })

    context = {
        'units': units,
        'representatives': representatives,
        'selected_representative': representative_id or '',
        'representative_name': representative_name,
    }
    return render(request, 'reports/ready_to_ship.html', context)


@login_required
@admin_or_manager_required
def delivery_list(request):
    """لیست اقلام آماده تحویل بر اساس تکمیل نقاشی + بسته‌بندی"""
    items = OrderItem.objects.filter(
        paint_tasks__isnull=False
    ).distinct().select_related(
        'order__customer',
        'order__user',
        'product__category'
    ).prefetch_related(
        'paint_tasks__painting_stage',
        'packaging_units',
        'ordercolor'
    )

    ready_items = []
    for item in items:
        if item.is_ready_for_delivery:
            ready_items.append({
                'item': item,
                'order': item.order,
                'product': item.product,
                'paint_done': item.paint_tasks.filter(status='done').count(),
                'paint_total': item.paint_tasks.count(),
                'packed': item.packaging_units.filter(is_packed=True).count(),
                'total_units': item.packaging_units.count(),
                'customer': item.order.customer,
                'representative': item.order.user,
            })

    representative_ids = {r['representative'].id for r in ready_items if r['representative']}
    representatives = User.objects.filter(id__in=representative_ids).distinct().order_by('username')

    selected_representative = request.GET.get('representative')
    if selected_representative:
        ready_items = [r for r in ready_items if r['representative'] and r['representative'].id == int(selected_representative)]

    context = {
        'ready_items': ready_items,
        'representatives': representatives,
        'selected_representative': selected_representative or '',
    }
    return render(request, 'delivery/delivery_list.html', context)


@login_required
@admin_or_manager_required
def delivery_confirm(request, item_id):
    """ثبت تحویل یک آیتم با تعیین مسیول و یادداشت"""
    item = get_object_or_404(OrderItem, pk=item_id)

    if not item.is_ready_for_delivery:
        messages.error(request, 'این آیتم هنوز برای تحویل آماده نیست.')
        return redirect('delivery_list')

    if request.method == 'POST':
        delivery_person_id = request.POST.get('delivery_person')
        notes = request.POST.get('notes', '').strip()
        plate = request.POST.get('plate', '').strip()

        if not delivery_person_id:
            messages.error(request, 'لطفاً مسیول تحویل را انتخاب کنید.')
            users = User.objects.filter(is_staff=True).order_by('username')
            context = {
                'item': item,
                'users': users,
                'selected_representative': request.POST.get('representative', ''),
            }
            return render(request, 'delivery/delivery_confirm.html', context)

        delivery_person = get_object_or_404(User, pk=delivery_person_id)

        units = item.packaging_units.filter(is_packed=True, is_shipped=False)
        with transaction.atomic():
            for unit in units:
                unit.is_shipped = True
                unit.shipped_at = timezone.now()
                unit.shipped_by = delivery_person
                unit.save()

                ShipmentLog.objects.create(
                    packaging_unit=unit,
                    plate_number=plate,
                    shipped_by=delivery_person,
                    delivery_notes=notes,
                )

            try:
                from .utils import log_production_event
                task = item.paint_tasks.filter(status='done').first()
                log_production_event(
                    task=task,
                    event_type='done',
                    user=delivery_person,
                    new_status='shipped',
                    quantity=item.quantity,
                )
            except Exception:
                pass

        messages.success(request, f'✅ تحویل {units.count()} واحد از آیتم {item.id} ثبت شد.')
        selected_representative = request.POST.get('representative', '')
        if selected_representative:
            return redirect(f"{reverse('delivery_list')}?representative={selected_representative}")
        return redirect('delivery_list')

    users = User.objects.filter(is_staff=True).order_by('username')
    context = {
        'item': item,
        'users': users,
    }
    return render(request, 'delivery/delivery_confirm.html', context)





















import json
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.forms import inlineformset_factory
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from .decorators import admin_or_manager_required
from .models import Product, ProductBOM, Part, Color
from .forms import ProductCreateForm, PartForm


# -------------------------------------------------------------------
# ویوهای محصول
# -------------------------------------------------------------------
@login_required
@admin_or_manager_required
def product_create(request):
    BOMFormSet = inlineformset_factory(
        Product, ProductBOM,
        fields=['part', 'quantity', 'color_part', 'color_material_map', 'size_adjustment_rule'],
        extra=1, can_delete=True,
        widgets={
            'part': forms.HiddenInput(),
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'color_part': forms.Select(attrs={'class': 'form-select color-part-select'}),
            'color_material_map': forms.HiddenInput(),
            'size_adjustment_rule': forms.HiddenInput(attrs={'class': 'size-rule-hidden'}),
        }
    )

    if request.method == 'POST':
        product_form = ProductCreateForm(request.POST)
        formset = BOMFormSet(request.POST, prefix='bom')

        if product_form.is_valid() and formset.is_valid():
            with transaction.atomic():
                product = product_form.save(commit=False)
                default_colors = {}
                for part, _ in Color.PART_CHOICES:
                    field_name = f'color_{part}'
                    code = product_form.cleaned_data.get(field_name)
                    if code:
                        default_colors[part] = code
                product.default_colors = default_colors
                product.save()

                instances = formset.save(commit=False)
                for bom in instances:
                    bom.product = product
                    bom.allow_material_override = bool(bom.color_part)
                    bom.size_affected = bool(bom.size_adjustment_rule)
                    if not bom.color_material_map:
                        bom.color_material_map = {}
                    bom.save()
                for obj in formset.deleted_objects:
                    obj.delete()

                messages.success(request, '✅ محصول و قطعات با موفقیت ذخیره شدند.')
                return redirect('admin_product_list')
    else:
        product_form = ProductCreateForm()
        formset = BOMFormSet(prefix='bom')

    context = {
        'product_form': product_form,
        'formset': formset,
        'product': None,
        'materials': Material.objects.all(),      
        'all_parts': Part.objects.all(),          
    }
    return render(request, 'product_create.html', context)


@login_required
@admin_or_manager_required
def product_edit(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    BOMFormSet = inlineformset_factory(
        Product, ProductBOM,
        fields=['part', 'quantity', 'color_part', 'color_material_map', 'size_adjustment_rule'],
        extra=0, can_delete=True,
        widgets={
            'part': forms.HiddenInput(),
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'color_part': forms.Select(attrs={'class': 'form-select color-part-select'}),
            'color_material_map': forms.HiddenInput(),
            'size_adjustment_rule': forms.HiddenInput(attrs={'class': 'size-rule-hidden'}),
        }
    )

    if request.method == 'POST':
        product_form = ProductCreateForm(request.POST, instance=product)
        formset = BOMFormSet(request.POST, prefix='bom', instance=product)

        if product_form.is_valid() and formset.is_valid():
            with transaction.atomic():
                product = product_form.save(commit=False)
                default_colors = {}
                for part, _ in Color.PART_CHOICES:
                    field_name = f'color_{part}'
                    code = product_form.cleaned_data.get(field_name)
                    if code:
                        default_colors[part] = code
                product.default_colors = default_colors
                product.save()

                instances = formset.save(commit=False)
                for bom in instances:
                    bom.product = product
                    bom.allow_material_override = bool(bom.color_part)
                    bom.size_affected = bool(bom.size_adjustment_rule)
                    if not bom.color_material_map:
                        bom.color_material_map = {}
                    bom.save()
                for obj in formset.deleted_objects:
                    obj.delete()

                messages.success(request, '✅ محصول با موفقیت ویرایش شد.')
                return redirect('admin_product_list')
    else:
        product_form = ProductCreateForm(instance=product)
        formset = BOMFormSet(prefix='bom', instance=product)

    context = {
        'product_form': product_form,
        'formset': formset,
        'product': product,
        'materials': Material.objects.all(),      
        'all_parts': Part.objects.all(),          
    }
    return render(request, 'product_create.html', context)


# -------------------------------------------------------------------
# ویوهای AJAX قطعه
# -------------------------------------------------------------------
@login_required
@admin_or_manager_required
def ajax_create_part(request):
    """ایجاد قطعه جدید از طریق AJAX"""
    if request.method == 'POST':
        form = PartForm(request.POST)
        if form.is_valid():
            part = form.save(commit=False)
            part.f2 = part.name
            part.save()
            return JsonResponse({'success': True, 'id': part.id, 'name': str(part)})
        return JsonResponse({'success': False, 'errors': form.errors})
    return JsonResponse({'success': False})


@login_required
@admin_or_manager_required
def ajax_get_part(request, part_id):
    """دریافت اطلاعات یک قطعه برای ویرایش (JSON)"""
    part = get_object_or_404(Part, pk=part_id)
    data = {
        'id': part.id,
        'name': part.name,
        'material': part.material_id,
        'length': str(part.length),
        'width': str(part.width),
        'grain': part.grain,
        'pname': part.pname,
        'turn': part.turn,
        'f26': part.f26,
        'f18': part.f18,
        'f4': part.f4,
        'f5': part.f5,
        'f3': part.f3,
        'routing_code': part.routing_code,
        'base_part': part.base_part_id,
    }
    return JsonResponse(data)


@login_required
@admin_or_manager_required
def ajax_edit_part(request, part_id):
    """ویرایش قطعه موجود از طریق AJAX"""
    part = get_object_or_404(Part, pk=part_id)
    if request.method == 'POST':
        form = PartForm(request.POST, instance=part)
        if form.is_valid():
            part = form.save(commit=False)
            part.f2 = part.name
            part.save()
            return JsonResponse({'success': True, 'id': part.id, 'name': str(part)})
        return JsonResponse({'success': False, 'errors': form.errors})
    return JsonResponse({'success': False})













from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.contrib import messages

@login_required
def set_plate(request):
    if request.method == 'POST':
        plate = request.POST.get('plate', '').strip()
        if plate:
            request.session['current_plate'] = plate
            messages.success(request, f'پلاک "{plate}" برای بارگیری فعال شد.')
        else:
            request.session.pop('current_plate', None)
            messages.warning(request, 'پلاک پاک شد.')
        return redirect('scan_part')   # می‌توانید به صفحهٔ اسکن یا هر جای دیگر هدایت کنید
    current_plate = request.session.get('current_plate', '')
    return render(request, 'set_plate.html', {'current_plate': current_plate})
















# views.py - بخش ارسال‌های مشتری

@login_required
def customer_shipments(request):
    """لیست ارسال‌های مشتری (گروه‌بندی شده بر اساس پلاک و تاریخ)"""
    # دریافت سفارش‌های کاربر
    orders = Order.objects.filter(user=request.user)
    
    # دریافت واحدهای ارسال‌شده مرتبط با سفارش‌های کاربر
    shipments = (
        PackagingUnit.objects
        .filter(order_item__order__in=orders, is_shipped=True)
        .select_related(
            'order_item__order__customer',
            'order_item__product__category',
            'order_item__order__user'
        )
        .prefetch_related('shipment_logs', 'order_item__ordercolor')
        .order_by('-shipped_at')
    )
    
    # گروه‌بندی بر اساس پلاک و تاریخ
    groups = {}
    for unit in shipments:
        log = unit.shipment_logs.first()
        if log:
            key = f"{log.plate_number}_{log.shipped_at.date()}"
            if key not in groups:
                groups[key] = {
                    'plate': log.plate_number,
                    'date': log.shipped_at,
                    'units': [],
                    'total_price': 0,
                }
            groups[key]['units'].append(unit)
            groups[key]['total_price'] += int(unit.order_item.unit_price or 0)
    
    # تبدیل به لیست برای مرتب‌سازی (جدیدترین اول)
    shipment_groups = list(groups.values())
    shipment_groups.sort(key=lambda x: x['date'], reverse=True)
    
    context = {
        'shipment_groups': shipment_groups,
    }
    return render(request, 'customer/shipments.html', context)


@login_required
def customer_shipment_detail(request, plate, date):
    """نمایش جزئیات یک سری ارسال (برگه ترخیص)"""
    from datetime import datetime
    import jdatetime
    
    try:
        ship_date = datetime.strptime(date, '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, 'تاریخ نامعتبر است.')
        return redirect('customer_shipments')
    
    # دریافت سفارش‌های کاربر
    orders = Order.objects.filter(user=request.user)
    
    # دریافت واحدهای ارسال‌شده با آن پلاک و تاریخ
    units = (
        PackagingUnit.objects
        .filter(
            order_item__order__in=orders,
            is_shipped=True,
            shipment_logs__plate_number=plate,
            shipped_at__date=ship_date
        )
        .select_related(
            'order_item__order__customer',
            'order_item__product__category',
            'order_item__order__user'
        )
        .prefetch_related('shipment_logs', 'order_item__ordercolor')
        .order_by('order_item__order__id', 'order_item__product__name')
    )
    
    if not units.exists():
        messages.warning(request, 'هیچ ارسالی با این مشخصات یافت نشد.')
        return redirect('customer_shipments')
    
    # اطلاعات نماینده و مشتری
    first_unit = units.first()
    representative = first_unit.order_item.order.user
    representative_name = representative.get_full_name() or representative.username
    customer_name = first_unit.order_item.order.customer.name
    
    # محاسبه جمع کل
    total_price = sum(int(u.order_item.unit_price or 0) for u in units)
    
    # تاریخ شمسی
    shamsi_date = jdatetime.date.fromgregorian(date=ship_date).strftime('%Y/%m/%d')
    
    context = {
        'units': units,
        'plate': plate,
        'date': ship_date,
        'shamsi_date': shamsi_date,
        'representative_name': representative_name,
        'customer_name': customer_name,
        'total_price': total_price,
    }
    return render(request, 'customer/shipment_detail.html', context)


# -------------------------------------------------------------------
#     روند نقاشی و برنامه‌ریزی روزانه
# -------------------------------------------------------------------

@login_required
@admin_or_manager_required
def assign_painting_process(request, item_id):
    from .utils import get_item_color_assignments, get_painting_process_for_color
    from .models import PaintingProcess, ProductionTask, create_paint_tasks

    item = get_object_or_404(OrderItem, pk=item_id)

    if request.method == 'POST':
        with transaction.atomic():
            existing_paint_tasks = ProductionTask.objects.filter(
                order=item.order,
                station_name='paint',
                order_item=item
            )
            if existing_paint_tasks.filter(status='done').exists():
                messages.error(
                    request,
                    "❌ برخی از مراحل نقاشی این آیتم قبلاً انجام شده‌اند. "
                    "برای جلوگیری از از دست رفتن سابقه، ابتدا آن‌ها را به صورت دستی مدیریت کنید."
                )
                return redirect('item_detail', pk=item_id)

            existing_paint_tasks.delete()

            global_base = ProductionTask.objects.filter(order=item.order).aggregate(
                max_step=models.Max('step_order')
            )['max_step'] or 0

            new_tasks = []
            errors = []

            assignments = get_item_color_assignments(item)

            if not assignments:
                errors.append("⚠️ هیچ کد رنگی برای این آیتم یافت نشد.")

            for part_name, color_code in assignments:
                painting_process = get_painting_process_for_color(color_code)
                if not painting_process:
                    errors.append(f"❌ {part_name} (کد رنگ {color_code}): روند نقاشی فعالی یافت نشد.")
                    continue

                create_paint_tasks(
                    tasks_list=new_tasks,
                    order=item.order,
                    quantity=item.quantity,
                    process=painting_process,
                    base_step=global_base,
                    order_item=item,
                    color_part=part_name
                )
                global_base += painting_process.stages.count()

            if new_tasks:
                created_tasks = ProductionTask.objects.bulk_create(new_tasks)
                # خودکارسازی درخواست مواد اولیه از فرمول ساخت (BOM)
                result = auto_create_material_issues(created_tasks, requested_by=request.user)
                if result['created']:
                    messages.info(
                        request,
                        f"🧾 {result['created']} درخواست مواد از انبار برای تسک‌های نقاشی ثبت شد."
                    )
                messages.success(
                    request,
                    f"✅ {len(new_tasks)} تسک نقاشی برای آیتم {item.id} ایجاد شد."
                )
                for err in errors:
                    messages.warning(request, err)
            else:
                for err in errors:
                    messages.error(request, err)
                messages.error(request, "❌ هیچ تسک نقاشی‌ای ایجاد نشد.")

        return redirect('item_detail', pk=item_id)

    color_codes = get_unique_color_codes_for_item(item)
    ROKESHI_CODES = {'8', '9', '10', '11'}
    color_info = [{'code': c, 'is_rokeshi': c in ROKESHI_CODES} for c in color_codes]
    return render(request, 'assign_painting.html', {
        'item': item,
        'color_info': color_info,
        'has_colors': item.ordercolor.exists() or bool(item.product.default_colors),
    })



@login_required
@admin_or_manager_required
def daily_schedule_print(request):
    """نمایش و چاپ برنامه روزانه کارگران نقاشی"""
    from django.db.models import Sum, Max
    from .models import ProductionTask, WorkerProfile, OrderItem
    import jdatetime

    all_days = request.GET.get('all_days', '') == '1'

    date_str = request.GET.get('date')
    if date_str:
        try:
            y, m, d = map(int, date_str.split('-'))
            selected_date = jdatetime.date(y, m, d)
        except (ValueError, TypeError):
            selected_date = jdatetime.date.today()
    else:
        selected_date = jdatetime.date.today()

    gregorian_date = selected_date.togregorian()
    today_gregorian = jdatetime.date.today().togregorian()

    if all_days:
        max_date_qs = ProductionTask.objects.filter(
            station_name='paint',
            scheduled_start__isnull=False,
        ).aggregate(max_date=Max('scheduled_start'))
        end_date_gregorian = max_date_qs['max_date'].date() if max_date_qs['max_date'] else gregorian_date

        tasks = list(
            ProductionTask.objects.filter(
                station_name='paint',
                scheduled_start__date__gte=today_gregorian,
                scheduled_start__date__lte=end_date_gregorian,
                scheduled_start__isnull=False,
            ).select_related(
                'order_item__product__category',
                'order_item__order__user',
                'painting_stage',
                'assigned_worker',
                'order_item__order__customer'
            ).order_by('assigned_worker_id', 'scheduled_start', 'step_order')
        )

        worker_date_tasks = {}
        for task in tasks:
            worker_id = task.assigned_worker_id
            if worker_id is None:
                continue
            task_date = task.scheduled_start.date()
            worker_date_tasks.setdefault(worker_id, {}).setdefault(task_date, []).append(task)

        worker_ids = list(worker_date_tasks.keys())
        workers = WorkerProfile.objects.filter(user_id__in=worker_ids).select_related('user')
        workers_map = {wp.user_id: wp for wp in workers}

        worker_columns = []
        for worker_id, date_tasks in worker_date_tasks.items():
            if worker_id is None:
                continue
            worker_profile = workers_map.get(worker_id)
            if worker_profile:
                user = worker_profile.user
                label = user.get_full_name() or user.username
                skills = worker_profile.skills or []
            else:
                label = f"کارگر #{worker_id}"
                skills = []

            total_duration = 0
            date_entries = []
            for task_date, worker_tasks in sorted(date_tasks.items()):
                duration = sum(
                    t.painting_stage.duration_minutes if t.painting_stage else (t.custom_duration_minutes or 0)
                    for t in worker_tasks
                )
                total_duration += duration
                date_entries.append({
                    'date': task_date,
                    'date_str': task_date.strftime('%Y-%m-%d'),
                    'tasks': worker_tasks,
                    'total_duration': duration,
                })

            worker_columns.append({
                'worker_id': worker_id,
                'label': label,
                'skills': skills,
                'date_entries': date_entries,
                'total_duration': total_duration,
                'task_count': sum(len(d['tasks']) for d in date_entries),
            })

        worker_columns.sort(key=lambda x: x['label'])

        end_date_jalali = None
        if max_date_qs['max_date']:
            end_date_jalali = jdatetime.date.fromgregorian(date=max_date_qs['max_date'])

        context = {
            'worker_columns': worker_columns,
            'all_days': True,
            'selected_date': selected_date,
            'selected_date_str': selected_date.strftime('%Y-%m-%d'),
            'gregorian_date': gregorian_date,
            'today_str': jdatetime.date.today().strftime('%Y/%m/%d'),
            'end_date': end_date_jalali,
        }
    else:
        tasks = list(
            ProductionTask.objects.filter(
                station_name='paint',
                scheduled_start__date=gregorian_date
            ).select_related(
                'order_item__product__category',
                'order_item__order__user',
                'painting_stage',
                'assigned_worker',
                'order_item__order__customer'
            ).order_by('assigned_worker_id', 'scheduled_start')
        )

        tasks_by_worker = {}
        for task in tasks:
            key = task.assigned_worker_id
            tasks_by_worker.setdefault(key, []).append(task)

        worker_ids = [k for k in tasks_by_worker if k is not None]
        workers = WorkerProfile.objects.filter(user_id__in=worker_ids).select_related('user')
        workers_map = {wp.user_id: wp for wp in workers}

        worker_columns = []
        for worker_id, worker_tasks in tasks_by_worker.items():
            if worker_id is None:
                continue
            worker_profile = workers_map.get(worker_id)
            if worker_profile:
                user = worker_profile.user
                label = user.get_full_name() or user.username
                skills = worker_profile.skills or []
            else:
                label = f"کارگر #{worker_id}"
                skills = []

            total_duration = sum(
                t.painting_stage.duration_minutes if t.painting_stage else (t.custom_duration_minutes or 0)
                for t in worker_tasks
            )

            worker_columns.append({
                'worker_id': worker_id,
                'label': label,
                'skills': skills,
                'tasks': worker_tasks,
                'total_duration': total_duration,
            })

        worker_columns.sort(key=lambda x: x['label'])

        context = {
            'worker_columns': worker_columns,
            'all_days': False,
            'selected_date': selected_date,
            'selected_date_str': selected_date.strftime('%Y-%m-%d'),
            'gregorian_date': gregorian_date,
            'today_str': jdatetime.date.today().strftime('%Y/%m/%d'),
            'yesterday': (selected_date - jdatetime.timedelta(days=1)).strftime('%Y-%m-%d'),
            'tomorrow': (selected_date + jdatetime.timedelta(days=1)).strftime('%Y-%m-%d'),
        }
    return render(request, 'daily_schedule_print.html', context)


@login_required
@admin_or_manager_required
def auto_assign_tasks_view(request):
    """اجرای دستی تخصیص خودکار کارگران به تسک‌های نقاشی"""
    if request.method == 'POST':
        from .utils import auto_assign_paint_tasks
        auto_assign_paint_tasks()
        messages.success(request, "تخصیص خودکار کارگران انجام شد.")
    return redirect('dashboard')


# -------------------------------------------------------------------
#     پنل مدیریت جامع نقاشی
# -------------------------------------------------------------------

@login_required
@admin_or_manager_required
def painting_management_dashboard(request):
    """داشبورد مدیریت نقاشی - صفحه اصلی با خلاصه اطلاعات"""
    from .utils import get_painting_ready_items_queryset, get_unscheduled_ready_items, painting_nav_context

    recent_tasks = ProductionTask.objects.filter(
        station_name='paint'
    ).select_related('order', 'part', 'painting_stage', 'assigned_worker', 'order_item__product').order_by('-id')[:10]

    context = {
        'active_tab': 'dashboard',
        'total_processes': PaintingProcess.objects.count(),
        'active_processes': PaintingProcess.objects.filter(is_active=True).count(),
        'total_stages': PaintingStage.objects.count(),
        'total_workers': WorkerProfile.objects.filter(stage='paint').count(),
        'pending_tasks': ProductionTask.objects.filter(station_name='paint', status__in=['pending', 'waiting']).count(),
        'unassigned_tasks': ProductionTask.objects.filter(station_name='paint', assigned_worker__isnull=True, status__in=['pending', 'waiting']).count(),
        'ready_items_count': get_painting_ready_items_queryset().count(),
        'unscheduled_ready_count': get_unscheduled_ready_items().count(),
        'recent_tasks': recent_tasks,
        **painting_nav_context(),
    }
    return render(request, 'painting_management/dashboard.html', context)


@login_required
@admin_or_manager_required
def painting_processes_view(request):
    """مدیریت روندهای نقاشی (لیست، ایجاد، ویرایش، حذف)"""
    from .models import PaintingProcess
    from .forms import PaintingProcessForm

    from .utils import painting_nav_context

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        action = request.POST.get('action')

        if action == 'create':
            form = PaintingProcessForm(request.POST)
            if form.is_valid():
                process = form.save()
                return JsonResponse({'success': True, 'id': process.id, 'name': process.name})
            return JsonResponse({'success': False, 'errors': form.errors})

        elif action == 'edit':
            process_id = request.POST.get('process_id')
            process = get_object_or_404(PaintingProcess, pk=process_id)
            form = PaintingProcessForm(request.POST, instance=process)
            if form.is_valid():
                form.save()
                return JsonResponse({'success': True})
            return JsonResponse({'success': False, 'errors': form.errors})

        elif action == 'delete':
            process_id = request.POST.get('process_id')
            process = get_object_or_404(PaintingProcess, pk=process_id)
            process.delete()
            return JsonResponse({'success': True})

        elif action == 'toggle_active':
            process_id = request.POST.get('process_id')
            process = get_object_or_404(PaintingProcess, pk=process_id)
            process.is_active = not process.is_active
            process.save()
            return JsonResponse({'success': True, 'is_active': process.is_active})

    # GET: نمایش لیست
    processes = PaintingProcess.objects.all().annotate(stage_count=Count('stages')).order_by('-is_active', 'name')

    # جستجو
    search = request.GET.get('search')
    if search:
        processes = processes.filter(Q(name__icontains=search) | Q(code__icontains=search))

    paginator = Paginator(processes, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'active_tab': 'processes',
        'processes': page_obj,
        'search': search,
        'form': PaintingProcessForm(),
        **painting_nav_context(),
    }
    return render(request, 'painting_management/processes.html', context)


@login_required
@admin_or_manager_required
def painting_process_detail_api(request, process_id):
    """بازگرداندن داده‌های یک روند برای فرم ویرایش (AJAX)"""
    from .models import PaintingProcess

    process = get_object_or_404(PaintingProcess, pk=process_id)
    return JsonResponse({
        'id': process.id,
        'name': process.name,
        'code': process.code,
        'color_codes': process.color_codes or [],
        'is_active': process.is_active,
        'description': process.description or '',
    })


@login_required
@admin_or_manager_required
def painting_stages_view(request, process_id=None):
    """مدیریت مراحل نقاشی برای یک روند خاص"""
    from .models import PaintingProcess, PaintingStage
    from .forms import PaintingStageForm

    from .utils import painting_nav_context

    process = None
    if process_id:
        process = get_object_or_404(PaintingProcess, pk=process_id)

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        action = request.POST.get('action')

        if action == 'create':
            form = PaintingStageForm(request.POST)
            if form.is_valid():
                stage = form.save()
                return JsonResponse({'success': True, 'id': stage.id, 'name': stage.name})
            return JsonResponse({'success': False, 'errors': form.errors})

        elif action == 'edit':
            stage_id = request.POST.get('stage_id')
            stage = get_object_or_404(PaintingStage, pk=stage_id)
            form = PaintingStageForm(request.POST, instance=stage)
            if form.is_valid():
                form.save()
                return JsonResponse({'success': True})
            return JsonResponse({'success': False, 'errors': form.errors})

        elif action == 'delete':
            stage_id = request.POST.get('stage_id')
            stage = get_object_or_404(PaintingStage, pk=stage_id)
            stage.delete()
            return JsonResponse({'success': True})

        elif action == 'reorder':
            stage_ids = request.POST.getlist('stage_ids[]')
            with transaction.atomic():
                stages = list(PaintingStage.objects.filter(pk__in=stage_ids))
                stage_map = {str(s.pk): s for s in stages}

                for offset, stage_id in enumerate(stage_ids, start=1):
                    stage = stage_map[str(stage_id)]
                    stage.order = -offset
                    stage.save(update_fields=['order'])

                for idx, stage_id in enumerate(stage_ids, start=1):
                    stage = stage_map[str(stage_id)]
                    stage.order = idx
                    stage.save(update_fields=['order'])

            return JsonResponse({'success': True})

    # GET: نمایش لیست مراحل
    stages = PaintingStage.objects.all()
    if process:
        stages = stages.filter(process=process)
    stages = stages.select_related('process').order_by('process__name', 'order')

    # جستجو
    search = request.GET.get('search')
    if search:
        stages = stages.filter(Q(name__icontains=search) | Q(process__name__icontains=search))

    paginator = Paginator(stages, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'active_tab': 'stages',
        'stages': page_obj,
        'process': process,
        'search': search,
        'form': PaintingStageForm(),
        'processes': PaintingProcess.objects.all(),
        **painting_nav_context(),
    }
    return render(request, 'painting_management/stages.html', context)


@login_required
@admin_or_manager_required
def painting_stage_detail_api(request, stage_id):
    """بازگرداندن داده‌های یک مرحله برای فرم ویرایش (AJAX)"""
    from .models import PaintingStage

    stage = get_object_or_404(PaintingStage, pk=stage_id)
    return JsonResponse({
        'id': stage.id,
        'process': stage.process_id,
        'order': stage.order,
        'name': stage.name,
        'duration_minutes': stage.duration_minutes,
        'drying_time_minutes': stage.drying_time_minutes,
         'required_skill': stage.required_skill,
    })


@login_required
@admin_or_manager_required
def painting_process_materials_api(request, process_id):
    """
    مدیریت کاتالوگ (بدون مقدار) مواد اولیه‌ی یک روند نقاشی.
    GET: لیست مواد فعلی کاتالوگ
    POST: افزودن یک ماده اولیه به کاتالوگ
    DELETE: حذف یک ماده از کاتالوگ (همراه با حذف تمام مقادیر مصرفِ ثبت‌شده
            برای این ماده در این روند، چون دیگر جزو این روند محسوب نمی‌شود)
    """
    from .models import PaintingProcess, PaintingProcessMaterial, PaintingMaterialRequirement

    process = get_object_or_404(PaintingProcess, pk=process_id)

    if request.method == 'GET':
        entries = PaintingProcessMaterial.objects.select_related('raw_material').prefetch_related('color_variants__raw_material').filter(process=process)
        return JsonResponse({
            'success': True,
            'process_id': process.id,
            'process_name': process.name,
            'materials': [
                {
                    'id': e.id,
                    'raw_material_id': e.raw_material_id,
                    'raw_material_name': str(e.raw_material),
                    'unit': e.raw_material.get_unit_display(),
                    'is_color_variant': e.is_color_variant,
                    'color_variant_count': e.color_variants.count(),
                }
                for e in entries
            ],
        })

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, TypeError):
            return JsonResponse({'success': False, 'error': 'داده ارسالی معتبر نیست'})
        raw_material_id = data.get('raw_material_id')
        if not raw_material_id:
            return JsonResponse({'success': False, 'error': 'ماده اولیه انتخاب نشده'})
        entry, created = PaintingProcessMaterial.objects.get_or_create(
            process=process, raw_material_id=raw_material_id
        )
        if 'is_color_variant' in data:
            value = data.get('is_color_variant')
            entry.is_color_variant = value if isinstance(value, bool) else str(value).lower() in ('1', 'true', 'yes', 'on')
            entry.save(update_fields=['is_color_variant'])
        return JsonResponse({
            'success': True,
            'created': created,
            'id': entry.id,
            'raw_material_id': entry.raw_material_id,
            'raw_material_name': str(entry.raw_material),
            'is_color_variant': entry.is_color_variant,
        })

    if request.method in ('PUT', 'PATCH'):
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, TypeError):
            return JsonResponse({'success': False, 'error': 'داده ارسالی معتبر نیست'})
        entry_id = data.get('id') or data.get('entry_id')
        entry = get_object_or_404(PaintingProcessMaterial, pk=entry_id, process=process)
        if 'raw_material_id' in data:
            entry.raw_material_id = data['raw_material_id']
        if 'is_color_variant' in data:
            value = data.get('is_color_variant')
            entry.is_color_variant = value if isinstance(value, bool) else str(value).lower() in ('1', 'true', 'yes', 'on')
        entry.save()
        return JsonResponse({
            'success': True,
            'id': entry.id,
            'raw_material_id': entry.raw_material_id,
            'raw_material_name': str(entry.raw_material),
            'is_color_variant': entry.is_color_variant,
        })

    if request.method == 'DELETE':
        entry_id = request.GET.get('id')
        entry = get_object_or_404(PaintingProcessMaterial, pk=entry_id, process=process)
        # حذف کامل: چون این ماده دیگر جزو کاتالوگ این روند نیست،
        # مقادیر مصرف ثبت‌شده برای آن هم بی‌معنی می‌شوند.
        PaintingMaterialRequirement.objects.filter(
            process=process, raw_material_id=entry.raw_material_id
        ).delete()
        entry.delete()
        return JsonResponse({'success': True})

    return JsonResponse({'success': False, 'error': 'روش غیرمجاز'})


@login_required
@admin_or_manager_required
def painting_process_material_variants_api(request, process_material_id):
    """مدیریت نگاشت کد رنگ به ماده واقعی برای یک اسلات رنگ‌وابسته."""
    from inventory.models import RawMaterial
    from .models import Color, PaintingColorMaterialVariant, PaintingProcessMaterial

    code_choices = get_color_code_choices()

    process_material = get_object_or_404(PaintingProcessMaterial, pk=process_material_id)

    if request.method == 'GET':
        variants = {
            variant.color_code: variant
            for variant in process_material.color_variants.select_related('raw_material__category')
        }
        return JsonResponse({
            'success': True,
            'process_material_id': process_material.id,
            'process_name': process_material.process.name,
            'raw_material_name': str(process_material.raw_material),
            'is_color_variant': process_material.is_color_variant,
            'color_choices': [
                {'code': code, 'label': label}
                for code, label in code_choices
            ],
            'variants': [
                {
                    'color_code': code,
                    'color_label': label,
                    'raw_material_id': variants[code].raw_material_id if code in variants else None,
                    'raw_material_name': str(variants[code].raw_material) if code in variants else '',
                    'unit': variants[code].raw_material.get_unit_display() if code in variants else '',
                }
                for code, label in code_choices
            ],
        })

    if request.method in ('POST', 'PUT', 'PATCH'):
        if not process_material.is_color_variant:
            return JsonResponse({'success': False, 'error': 'این اسلات به کد رنگ وابسته نیست.'})
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, TypeError):
            return JsonResponse({'success': False, 'error': 'داده ارسالی معتبر نیست'})

        color_code = str(data.get('color_code', '')).strip()
        if color_code not in dict(code_choices):
            return JsonResponse({'success': False, 'error': 'کد رنگ معتبر نیست'})

        raw_material_id = data.get('raw_material_id')
        if not raw_material_id:
            return JsonResponse({'success': False, 'error': 'ماده اولیه واقعی انتخاب نشده'})
        raw_material = get_object_or_404(RawMaterial, pk=raw_material_id)
        if not raw_material.is_active:
            return JsonResponse({'success': False, 'error': 'ماده اولیه غیرفعال است'})

        variant, created = PaintingColorMaterialVariant.objects.update_or_create(
            process_material=process_material,
            color_code=color_code,
            defaults={'raw_material': raw_material},
        )
        return JsonResponse({
            'success': True,
            'created': created,
            'id': variant.id,
            'color_code': variant.color_code,
            'color_label': variant.get_color_code_display(),
            'raw_material_id': variant.raw_material_id,
            'raw_material_name': str(variant.raw_material),
            'unit': variant.raw_material.get_unit_display(),
        })

    if request.method == 'DELETE':
        color_code = request.GET.get('color_code')
        variant = get_object_or_404(
            PaintingColorMaterialVariant,
            process_material=process_material,
            color_code=color_code,
        )
        variant.delete()
        return JsonResponse({'success': True})

    return JsonResponse({'success': False, 'error': 'روش غیرمجاز'})


@login_required
@admin_or_manager_required
def product_color_part_materials_api(request):
    """
    مدیریت مقدار مصرف مواد اولیه برای یک (محصول + بخش رنگی) مشخص.

    GET: برای هر روند فعال، تمام مواد کاتالوگ آن روند را ردیف‌به‌ردیف نشان
         می‌دهد. اگر مقداری قبلاً برای این (محصول+بخش رنگی) تعیین شده
         باشد، مقدار را می‌آورد؛ در غیر این صورت is_set=False برمی‌گرداند
         تا در UI به‌عنوان «تعیین‌نشده» مشخص شود.
    POST: مقدار مصرف یک (process, raw_material) را برای این (product,
          color_part) ثبت/به‌روزرسانی می‌کند.
    DELETE: مقدار تعیین‌شده را حذف می‌کند (بازگشت به حالت «تعیین‌نشده»).
    """
    from .models import (
        Product, PaintingProcess, PaintingProcessMaterial, PaintingMaterialRequirement,
    )

    if request.method == 'GET':
        product_id = request.GET.get('product_id')
        color_part = request.GET.get('color_part')
        if not product_id or not color_part:
            return JsonResponse({'success': False, 'error': 'product_id و color_part الزامی هستند'})

        product = get_object_or_404(Product, pk=product_id)

        existing = {
            (r.process_id, r.raw_material_id): r
            for r in PaintingMaterialRequirement.objects.filter(
                product_id=product_id, color_part=color_part
            )
        }

        rows = []
        for process in PaintingProcess.objects.filter(is_active=True).order_by('name'):
            catalog = PaintingProcessMaterial.objects.select_related('raw_material').prefetch_related('color_variants').filter(process=process)
            for entry in catalog:
                req = existing.get((process.id, entry.raw_material_id))
                rows.append({
                    'process_id': process.id,
                    'process_name': process.name,
                    'process_material_id': entry.id,
                    'raw_material_id': entry.raw_material_id,
                    'raw_material_name': str(entry.raw_material),
                    'unit': entry.raw_material.get_unit_display(),
                    'is_color_variant': entry.is_color_variant,
                    'color_variant_count': entry.color_variants.count(),
                    'consumption_per_unit': str(req.consumption_per_unit) if req else '',
                    'is_set': req is not None,
                    'requirement_id': req.id if req else None,
                })

        return JsonResponse({
            'success': True,
            'product_id': product_id,
            'product_name': product.name,
            'color_part': color_part,
            'rows': rows,
        })

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, TypeError):
            return JsonResponse({'success': False, 'error': 'داده ارسالی معتبر نیست'})

        product_id = data.get('product_id')
        color_part = data.get('color_part')
        process_id = data.get('process_id')
        raw_material_id = data.get('raw_material_id')
        consumption = data.get('consumption_per_unit', 0)

        if not all([product_id, color_part, process_id, raw_material_id]):
            return JsonResponse({'success': False, 'error': 'همه فیلدها (محصول، بخش رنگی، روند، ماده اولیه) الزامی هستند'})

        try:
            consumption = float(consumption)
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'error': 'مقدار مصرف نامعتبر'})

        if not PaintingProcessMaterial.objects.filter(
            process_id=process_id, raw_material_id=raw_material_id
        ).exists():
            return JsonResponse({
                'success': False,
                'error': 'این ماده در کاتالوگ این روند تعریف نشده است. ابتدا از صفحهٔ «روندها» آن را به کاتالوگ اضافه کنید.',
            })

        req_obj, created = PaintingMaterialRequirement.objects.update_or_create(
            process_id=process_id,
            raw_material_id=raw_material_id,
            product_id=product_id,
            color_part=color_part,
            defaults={'consumption_per_unit': consumption},
        )
        return JsonResponse({
            'success': True,
            'id': req_obj.id,
            'consumption_per_unit': str(req_obj.consumption_per_unit),
        })

    if request.method == 'DELETE':
        req_id = request.GET.get('id')
        if not req_id:
            return JsonResponse({'success': False, 'error': 'شناسه رکورد مشخص نشده'})
        req_obj = get_object_or_404(PaintingMaterialRequirement, pk=req_id)
        req_obj.delete()
        return JsonResponse({'success': True, 'message': 'مقدار حذف شد و به حالت «تعیین‌نشده» بازگشت.'})

    return JsonResponse({'success': False, 'error': 'روش غیرمجاز'})


@login_required
@admin_or_manager_required
def search_raw_materials_api(request):
    """API جستجوی مواد اولیه برای Select2 در پنل نقاشی."""
    from inventory.models import RawMaterial
    q = request.GET.get('q', '').strip()
    materials = RawMaterial.objects.select_related('category').filter(is_active=True)
    if q:
        materials = materials.filter(
            Q(name__icontains=q) | Q(code__icontains=q) | Q(category__name__icontains=q)
        )
    materials = materials.order_by('category__name', 'name')[:20]
    results = [{
        'id': m.id,
        'text': f"{m.name} ({m.category.name})",
        'unit': m.get_unit_display(),
    } for m in materials]
    return JsonResponse({'results': results})


@login_required
@admin_or_manager_required
def painting_workers_view(request):
    """مدیریت کارگران نقاشی و مهارت‌هایشان"""
    import json
    from .models import WorkerProfile
    from .forms import WorkerProfileForm

    from .utils import painting_nav_context

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        action = request.POST.get('action')

        if action == 'create':
            form = WorkerProfileForm(request.POST)
            if form.is_valid():
                worker = form.save()
                return JsonResponse({'success': True, 'id': worker.id, 'name': worker.user.username})
            return JsonResponse({'success': False, 'errors': form.errors})

        elif action == 'edit':
            worker_id = request.POST.get('worker_id')
            worker = get_object_or_404(WorkerProfile, pk=worker_id)
            form = WorkerProfileForm(request.POST, instance=worker)
            if form.is_valid():
                form.save()
                return JsonResponse({'success': True})
            return JsonResponse({'success': False, 'errors': form.errors})

        elif action == 'delete':
            worker_id = request.POST.get('worker_id')
            worker = get_object_or_404(WorkerProfile, pk=worker_id)
            worker.delete()
            return JsonResponse({'success': True})

        elif action == 'assign_skills':
            # به‌روزرسانی JSON مهارت‌ها
            worker_id = request.POST.get('worker_id')
            worker = get_object_or_404(WorkerProfile, pk=worker_id)
            skills = request.POST.get('skills', '[]')
            try:
                worker.skills = json.loads(skills)
                worker.save()
                return JsonResponse({'success': True})
            except json.JSONDecodeError:
                return JsonResponse({'success': False, 'error': 'فرمت JSON نامعتبر'})

        elif action == 'assign_excluded_products':
            # مدیریت محصولات ممنوعه برای کارگر
            worker_id = request.POST.get('worker_id')
            product_ids = request.POST.get('excluded_products', '')
            worker = get_object_or_404(WorkerProfile, pk=worker_id)
            try:
                ids = [int(x) for x in product_ids.split(',') if x.strip()]
                worker.excluded_products.set(ids)
                return JsonResponse({'success': True})
            except (ValueError, TypeError):
                return JsonResponse({'success': False, 'error': 'داده‌های نامعتبر'})

        elif action == 'assign_excluded_items':
            worker_id = request.POST.get('worker_id')
            worker = get_object_or_404(WorkerProfile, pk=worker_id)
            item_ids = request.POST.getlist('excluded_items')
            try:
                item_ids = [int(iid) for iid in item_ids if iid]
                worker.excluded_items.set(item_ids)
                return JsonResponse({'success': True, 'message': 'آیتم‌های ممنوعه ذخیره شدند.'})
            except (ValueError, TypeError):
                return JsonResponse({'success': False, 'error': 'شناسه‌های آیتم نامعتبر'})

    # GET: نمایش لیست کارگران
    workers = WorkerProfile.objects.filter(stage='paint').select_related('user').annotate(
        active_tasks=Count(
            'user__assigned_tasks',
            filter=Q(user__assigned_tasks__station_name='paint', user__assigned_tasks__status__in=['pending', 'waiting'])
        )
    ).order_by('user__username')

    # جستجو و فیلتر
    search = request.GET.get('search')
    status = request.GET.get('status', '')
    skill_filter = request.GET.get('skill', '')
    if search:
        workers = workers.filter(
            Q(user__username__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
        )
    if status:
        if status == 'active':
            workers = workers.filter(is_available=True)
        elif status == 'inactive':
            workers = workers.filter(is_available=False)
    if skill_filter:
        workers = workers.filter(skills__contains=[skill_filter])

    paginator = Paginator(workers, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    all_products = Product.objects.select_related('category').order_by('category__name', 'name')

    context = {
        'active_tab': 'workers',
        'workers': page_obj,
        'search': search,
        'status': status,
        'skill_filter': skill_filter,
        'form': WorkerProfileForm(),
        'skill_choices': PaintingStage.SKILL_CHOICES,
        'all_products': Product.objects.all().order_by('name'),
        'all_products': all_products,
        'all_items': OrderItem.objects.select_related('product', 'order').filter(
            order__status__in=['planned', 'producing']
        ).order_by('-order__id'),
        'user_list': User.objects.filter(is_active=True).order_by('username'),
        **painting_nav_context(),
    }
    return render(request, 'painting_management/workers.html', context)


@login_required
@admin_or_manager_required
def painting_worker_excluded_items(request, worker_id):
    """نمایش آیتم‌های سفارشی که این کارگر نباید در آنها کار کند"""
    from .utils import painting_nav_context

    worker = get_object_or_404(WorkerProfile, pk=worker_id, stage='paint')
    excluded_products = worker.excluded_products.all()

    # آیتم‌هایی که مستقیماً برای این کارگر ممنوع شده‌اند
    direct_excluded_items = list(worker.excluded_items.all())

    # کلیدها برای جلوگیری از تکرار
    direct_ids = {x.id for x in direct_excluded_items}

    # آیتم‌هایی که به دلیل ممنوع بودن محصولشان ممنوع می‌شوند (فقط سفارش‌های فعال)
    product_excluded_items = list(
        OrderItem.objects.filter(
            product__in=excluded_products,
            order__status__in=['planned', 'producing'],
        ).exclude(pk__in=direct_ids).select_related('order', 'product', 'order__customer')
    )

    # نشانه‌گذاری منبع ممنوعیت و ترکیب
    for x in direct_excluded_items:
        x.ban_reason = 'item'
    for x in product_excluded_items:
        x.ban_reason = 'product'

    combined = direct_excluded_items + product_excluded_items
    combined.sort(key=lambda x: getattr(x, 'order_id', 0) or 0, reverse=True)

    page_number = request.GET.get('page')
    paginator = Paginator(combined, 50)
    excluded_items_page = paginator.get_page(page_number)

    context = {
        'worker': worker,
        'excluded_products': excluded_products,
        'excluded_items': excluded_items_page,
        'excluded_count': len(combined),
        'direct_count': len(direct_excluded_items),
        'product_count': len(product_excluded_items),
        **painting_nav_context(),
    }
    return render(request, 'painting_management/worker_excluded_items.html', context)


@login_required
@admin_or_manager_required
def painting_schedule_view(request):
    from .utils import get_unscheduled_ready_items, parse_jalali_date, painting_nav_context

    date_str = request.GET.get('date')
    if date_str:
        try:
            selected_date = parse_jalali_date(date_str)
        except ValueError:
            selected_date = jdatetime.date.today()
    else:
        selected_date = jdatetime.date.today()

    gregorian_date = selected_date.togregorian()

    tasks = list(
        ProductionTask.objects.filter(
            station_name='paint',
            scheduled_start__date=gregorian_date,
            part__isnull=True,
        ).select_related(
            'order_item__order', 'order_item__product', 'order_item__product__category',
            'painting_stage', 'assigned_worker',
        ).prefetch_related('order_item__ordercolor').order_by('scheduled_start', 'step_order')
    )

    workers = list(WorkerProfile.objects.filter(stage='paint', is_available=True).select_related('user'))

    tasks_by_worker = {}
    for t in tasks:
        tasks_by_worker.setdefault(t.assigned_worker_id, []).append(t)

    unscheduled_tasks = list(
        ProductionTask.objects.filter(
            station_name='paint',
            scheduled_start__isnull=True,
            status__in=['pending', 'waiting'],
            part__isnull=True,
        ).select_related(
            'order_item__order', 'order_item__product', 'order_item__product__category',
            'painting_stage', 'assigned_worker',
        ).prefetch_related('order_item__ordercolor').order_by('step_order')
    )

    worker_columns = [{
        'worker_id': '__unscheduled__',
        'label': 'بدون برنامه‌ریزی',
        'skills': [],
        'tasks': unscheduled_tasks,
        'is_unscheduled': True,
    }] + [{
        'worker_id': wp.user_id,
        'label': wp.user.get_full_name() or wp.user.username,
        'skills': wp.skills or [],
        'tasks': tasks_by_worker.get(wp.user_id, []),
    } for wp in workers]

    unassigned_tasks = tasks_by_worker.get(None, [])
    ready_unscheduled = get_unscheduled_ready_items()

    stats = {
        'total_tasks': len(tasks),
        'assigned_tasks': sum(1 for t in tasks if t.assigned_worker_id),
        'unassigned_tasks': len(unassigned_tasks),
        'ready_unscheduled': ready_unscheduled.count(),
        'total_duration': sum(t.painting_stage.duration_minutes if t.painting_stage else (t.custom_duration_minutes or 0) for t in tasks),
    }

    context = {
        'active_tab': 'schedule',
        'worker_columns': worker_columns,
        'unassigned_tasks': unassigned_tasks,
        'ready_unscheduled': ready_unscheduled,
        'selected_date': selected_date,
        'selected_date_str': selected_date.strftime('%Y-%m-%d'),
        'stats': stats,
        'yesterday': (selected_date - jdatetime.timedelta(days=1)).strftime('%Y-%m-%d'),
        'tomorrow': (selected_date + jdatetime.timedelta(days=1)).strftime('%Y-%m-%d'),
        'schedule_date': selected_date.strftime('%Y-%m-%d'),
        'workers': workers,
        **painting_nav_context(),
    }
    return render(request, 'painting_management/schedule.html', context)


@login_required
@admin_or_manager_required
def painting_ready_list(request):
    """آیتم‌های آماده نقاشی با پیش‌نمایش وضعیت و جستجوی یکپارچه"""
    from .utils import get_painting_ready_items_queryset, painting_nav_context, get_item_paint_preview

    # فقط پارامتر جستجو (بدون نماینده جداگانه)
    search = request.GET.get('search', '')
    process_id = request.GET.get('process')

    ready_items = get_painting_ready_items_queryset(search=search, process_id=process_id)

    items_with_preview = [
        {'item': item, 'preview': get_item_paint_preview(item)}
        for item in ready_items
    ]

    context = {
        'active_tab': 'ready',
        'items_with_preview': items_with_preview,
        'search': search,
        'processes': PaintingProcess.objects.filter(is_active=True),
        'selected_process': process_id,
        'schedule_date': jdatetime.date.today().strftime('%Y-%m-%d'),
        **painting_nav_context(),
    }
    return render(request, 'painting_management/ready_list.html', context)



@login_required
@admin_or_manager_required
def painting_add_to_schedule(request):
    """
    افزودن خودکار آیتم‌های انتخاب‌شده به برنامه امروز + تخصیص خودکار کارگر
    """
    # ================== بررسی درخواست ==================
    if request.method != 'POST' or request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})

    # ================== دریافت و تبدیل آیتم‌ها ==================
    item_ids_raw = request.POST.getlist('item_ids[]') or request.POST.getlist('item_ids')
    if not item_ids_raw:
        return JsonResponse({'success': False, 'error': 'هیچ آیتمی انتخاب نشده است'})

    # تبدیل به لیست اعداد صحیح (فقط مقادیر عددی معتبر)
    item_ids = []
    for val in item_ids_raw:
        try:
            item_ids.append(int(val))
        except (ValueError, TypeError):
            continue  # مقادیر غیرعددی نادیده گرفته شوند

    if not item_ids:
        return JsonResponse({'success': False, 'error': 'شناسه‌های آیتم نامعتبر هستند'})

    # ================== دریافت و تبدیل تاریخ ==================
    date_str = request.POST.get('date') or request.POST.get('target_date')
    target_date = None

    if date_str:
        try:
            # استفاده از parse_jalali_date (که باید حتماً jdatetime.date برگرداند یا استثنا پرتاب کند)
            target_date = parse_jalali_date(date_str)
        except Exception as e:
            logger.error(f"خطا در parse_jalali_date: {e}")
            # اگر خطا رخ داد، تاریخ امروز را به‌عنوان جایگزین استفاده می‌کنیم
            target_date = jdatetime.date.today()
            # اما لاگ می‌کنیم تا بدانیم مشکلی وجود دارد
    else:
        target_date = jdatetime.date.today()

    # اطمینان از اینکه target_date از نوع jdatetime.date است
    if not isinstance(target_date, jdatetime.date):
        logger.warning(f"target_date از نوع {type(target_date)} است، جایگزین با امروز")
        target_date = jdatetime.date.today()

    # ================== فراخوانی تابع اصلی ==================
    try:
        result = create_and_schedule_items_for_date(item_ids, target_date)
    except Exception as e:
        error_trace = traceback.format_exc()
        logger.error(f"خطا در create_and_schedule_items_for_date: {e}\n{error_trace}")
        # برگرداندن خطا به کاربر (در صورت محیط توسعه، می‌توان traceback را هم نمایش داد)
        return JsonResponse({
            'success': False,
            'error': f'خطای داخلی: {str(e)}',
            # 'traceback': error_trace  # اختیاری
        })

    # ================== تحلیل نتیجه ==================
    created_count = len(result.get('created_items') or [])
    scheduled_count = result.get('scheduled_count') or 0
    scheduled_date = result.get('scheduled_date') or target_date
    skipped = result.get('skipped') or []

    if created_count == 0 and scheduled_count == 0 and not skipped:
        return JsonResponse({
            'success': False,
            'error': 'هیچ عملیاتی انجام نشد. احتمالاً آیتم‌ها قبلاً تسک دارند یا رنگ معتبری ندارند.'
        })

    # در صورت زمان‌بندی، تخصیص خودکار کارگران انجام شود
    if scheduled_count > 0:
        try:
            auto_assign_paint_tasks(target_date=scheduled_date)
        except Exception as e:
            logger.error(f"خطا در auto_assign_paint_tasks: {e}")

    # ================== ساخت پیام ==================
    message_parts = []
    if created_count:
        message_parts.append(f"{created_count} آیتم تسک جدید ایجاد شد")
    if scheduled_count:
        message_parts.append(f"{scheduled_count} تسک زمان‌بندی شد")
    if skipped:
        reasons = {'no_color': 'بدون رنگ', 'no_process_match': 'روند نقاشی یافت نشد'}
        detail = '؛ '.join(f"آیتم {s['item_id']}: {reasons.get(s['reason'], 'نامشخص')}" for s in skipped[:3])
        if len(skipped) > 3:
            detail += f' و {len(skipped)-3} مورد دیگر'
        message_parts.append(f"{len(skipped)} آیتم رد شد ({detail})")

    message = ' + '.join(message_parts) if message_parts else 'همه آیتم‌ها قبلاً برنامه‌ریزی شده بودند.'

    # ================== پاسخ نهایی ==================
    return JsonResponse({
        'success': True,
        'message': message,
        'redirect': reverse('painting_schedule') + f'?date={scheduled_date.strftime("%Y-%m-%d")}',
    })


@login_required
@admin_or_manager_required
def painting_create_custom_task(request):
    """ایجاد یک کارت دلخواه (بدون سفارش واقعی) و تخصیص اختیاری به یک کارگر"""
    if request.method != 'POST' or request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})

    title = request.POST.get('title', '').strip()
    note = request.POST.get('note', '').strip()
    worker_id = request.POST.get('worker_id') or None
    date_str = request.POST.get('date') or None

    try:
        duration = int(request.POST.get('duration_minutes') or 60)
    except (TypeError, ValueError):
        duration = 60
    if duration <= 0:
        duration = 60

    if not title:
        return JsonResponse({'success': False, 'error': 'عنوان کارت الزامی است'})

    with transaction.atomic():
        customer, _ = Customer.objects.get_or_create(
            user=request.user,
            name='کار متفرقه',
            defaults={'phone': '', 'address': ''}
        )
        order = Order.objects.create(
            user=request.user,
            customer=customer,
            number='دلخواه',
            status='draft',
        )
        task = ProductionTask.objects.create(
            order=order,
            part=None,
            station_name='paint',
            step_order=1,
            quantity=1,
            status='pending',
            painting_stage=None,
            order_item=None,
            color_part='',
            custom_title=title,
            custom_duration_minutes=duration,
            custom_note=note,
        )

    if worker_id:
        result = assign_task_to_worker(task.id, worker_id, target_date=date_str)
        if not result.get('ok'):
            return JsonResponse({
                'success': True,
                'task_id': task.id,
                'assigned': False,
                'warning': result.get('error', 'تخصیص خودکار ناموفق بود؛ کارت بدون برنامه‌ریزی ایجاد شد.'),
            })
        return JsonResponse({'success': True, 'task_id': task.id, 'assigned': True})

    return JsonResponse({'success': True, 'task_id': task.id, 'assigned': False})


@login_required
@admin_or_manager_required
def painting_assign_process(request):
    """تخصیص خودکار روند نقاشی به یک آیتم سفارش بر اساس کدهای رنگی (AJAX)"""
    from .utils import get_item_color_assignments, get_painting_process_for_color

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        item_id = request.POST.get('item_id')

        if not item_id:
            return JsonResponse({'success': False, 'error': 'اطلاعات ناقص'})

        item = get_object_or_404(OrderItem, pk=item_id)

        try:
            with transaction.atomic():
                existing = ProductionTask.objects.filter(order=item.order, station_name='paint', order_item=item)
                if existing.filter(status='done').exists():
                    return JsonResponse({'success': False, 'error': 'برخی مراحل قبلاً انجام شده‌اند؛ امکان بازسازی خودکار نیست.'})
                existing.delete()

                global_base = ProductionTask.objects.filter(order=item.order).aggregate(
                    max_step=models.Max('step_order')
                )['max_step'] or 0

                assignments = get_item_color_assignments(item)
                new_tasks = []

                for part_name, color_code in assignments:
                    painting_process = get_painting_process_for_color(color_code)
                    if not painting_process:
                        continue

                    create_paint_tasks(
                        new_tasks, item.order, item.quantity,
                        painting_process, global_base, order_item=item, color_part=part_name
                    )
                    global_base += painting_process.stages.count()

                if new_tasks:
                    created_tasks = ProductionTask.objects.bulk_create(new_tasks)
                    # خودکارسازی درخواست مواد اولیه از فرمول ساخت (BOM)
                    result = auto_create_material_issues(created_tasks, requested_by=request.user)
                    if result['created']:
                        messages.info(
                            request,
                            f"🧾 {result['created']} درخواست مواد از انبار برای تسک‌های نقاشی ثبت شد."
                        )

                return JsonResponse({'success': True, 'message': 'روند نقاشی با موفقیت اعمال شد.'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

    return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})


@login_required
@admin_or_manager_required
def painting_auto_assign(request):
    """اجرای تخصیص خودکار کارگران (AJAX)"""
    from .utils import auto_assign_paint_tasks, parse_jalali_date

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            target_date = None
            if request.POST.get('date'):
                target_date = parse_jalali_date(request.POST.get('date'))

            assigned_count = auto_assign_paint_tasks(target_date=target_date)

            return JsonResponse({
                'success': True,
                'message': 'تخصیص خودکار با موفقیت انجام شد.',
                'assigned_count': assigned_count or 0,
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

    return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})


@login_required
@admin_or_manager_required
def painting_get_available_workers(request):
    """دریافت لیست کارگران با مهارت خاص (AJAX) برای انتخاب دستی"""
    from .models import WorkerProfile

    skill = request.GET.get('skill')
    if not skill:
        return JsonResponse({'workers': []})

    # فیلتر در سمت پایتون (skills__contains روی SQLite پشتیبانی نمی‌شود)
    workers = WorkerProfile.objects.filter(stage='paint').select_related('user')
    data = [{
        'id': w.user.id,
        'name': w.user.get_full_name() or w.user.username,
        'active_tasks': w.user.assigned_tasks.filter(status__in=['pending', 'waiting']).count()
    } for w in workers if skill in (w.skills or [])]

    return JsonResponse({'workers': data})

@login_required
@admin_or_manager_required
def painting_assign_worker(request):
    """تخصیص دستی یک کارگر به یک تسک نقاشی (AJAX) — با استفاده از assign_task_to_worker"""
    from django.http import JsonResponse
    import traceback

    if request.method != 'POST' or request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})

    task_id = request.POST.get('task_id')
    worker_id = request.POST.get('worker_id')
    target_date = request.POST.get('target_date')
    allow_overtime = request.POST.get('allow_overtime') == 'true'

    if not task_id or not worker_id:
        return JsonResponse({'success': False, 'error': 'اطلاعات ناقص (task_id یا worker_id ارسال نشده)'})

    try:
        result = assign_task_to_worker(task_id, worker_id, target_date=target_date, allow_overtime=allow_overtime)

        if result.get('ok'):
            return JsonResponse({
                'success': True,
                'message': f"تسک به کارگر تخصیص یافت ({result.get('scheduled_start')} تا {result.get('scheduled_end')})",
                'scheduled_start': result.get('scheduled_start'),
                'scheduled_end': result.get('scheduled_end'),
            })
        else:
            return JsonResponse({
                'success': False,
                'error': result.get('error', 'خطای ناشناخته'),
                'requires_overtime_confirmation': result.get('requires_overtime_confirmation', False),
            })

    except Exception as e:
        logger.error(f"خطا در painting_assign_worker: {e}\n{traceback.format_exc()}")
        return JsonResponse({
            'success': False,
            'error': f'خطای داخلی: {str(e)}'
        })


@login_required
@admin_or_manager_required
def painting_unassign_worker(request):
    """حذف تخصیص کارگر از یک تسک — برای درگ به ستون «بدون تخصیص»"""
    from .models import ProductionTask
    from .utils import reschedule_worker_tasks_on_date, parse_jalali_date

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        task_id = request.POST.get('task_id')
        if not task_id:
            return JsonResponse({'success': False, 'error': 'اطلاعات ناقص'})
        task = get_object_or_404(ProductionTask, pk=task_id, station_name='paint')
        old_worker_id = task.assigned_worker_id
        old_scheduled_start = task.scheduled_start
        target_date = None
        if old_scheduled_start:
            try:
                target_date = jdatetime.date.fromgregorian(date=old_scheduled_start.date())
            except Exception:
                target_date = None
        task.assigned_worker = None
        task.scheduled_start = None
        task.scheduled_end = None
        task.save()

        if old_worker_id and target_date:
            try:
                reschedule_worker_tasks_on_date(old_worker_id, target_date)
            except Exception as e:
                logger.warning(f"خطا در بازنشانی تسک‌های کارگر {old_worker_id} در تاریخ {target_date}: {e}")

        return JsonResponse({'success': True})

    return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})


@login_required
@admin_or_manager_required
def painting_delete_tasks(request):
    """حذف چند تسک نقاشی انتخاب‌شده (AJAX) — تسک‌های تکمیل‌شده حذف نمی‌شوند.
    ورودی: task_ids[] (حذف تسک‌های مشخص) یا item_ids[] (حذف همه تسک‌های نقاشی آیتم‌ها)
    """
    from .models import ProductionTask

    if request.method != 'POST' or request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})

    task_ids = request.POST.getlist('task_ids[]') or request.POST.getlist('task_ids')
    item_ids = request.POST.getlist('item_ids[]') or request.POST.getlist('item_ids')

    if not task_ids and not item_ids:
        return JsonResponse({'success': False, 'error': 'هیچ موردی انتخاب نشده است'})

    qs = ProductionTask.objects.filter(station_name='paint', status__in=['pending', 'waiting'])
    if item_ids:
        try:
            item_ids = [int(i) for i in item_ids]
        except (ValueError, TypeError):
            return JsonResponse({'success': False, 'error': 'شناسه‌های آیتم نامعتبر'})
        qs = qs.filter(order_item_id__in=item_ids)
    else:
        try:
            task_ids = [int(t) for t in task_ids]
        except (ValueError, TypeError):
            return JsonResponse({'success': False, 'error': 'شناسه‌های نامعتبر'})
        qs = qs.filter(pk__in=task_ids)

    deleted = qs.delete()[0]

    return JsonResponse({
        'success': True,
        'message': f'{deleted} تسک نقاشی حذف شد.',
        'deleted_count': deleted,
    })


@login_required
@admin_or_manager_required
def painting_clear_schedule(request):
    """پاک کردن تمام برنامه‌ریزی‌های روز انتخاب‌شده (AJAX)"""
    from .utils import parse_jalali_date

    if request.method != 'POST' or request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})

    date_str = request.POST.get('date')
    if not date_str:
        return JsonResponse({'success': False, 'error': 'تاریخ ارسال نشده'})

    try:
        target_date = parse_jalali_date(date_str)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)})

    gregorian = target_date.togregorian()
    tasks = ProductionTask.objects.filter(
        station_name='paint',
        scheduled_start__date=gregorian,
    )
    count = tasks.count()
    tasks.update(scheduled_start=None, scheduled_end=None, assigned_worker=None)
    return JsonResponse({
        'success': True,
        'message': f'{count} تسک از برنامه {target_date.strftime("%Y/%m/%d")} حذف شد.',
        'cleared_count': count,
    })


@login_required
@admin_or_manager_required
def painting_reset_schedule(request):
    """بازنشانی زمان‌بندی روز انتخاب‌شده به حالت اولیه قبل از ویرایش دستی"""
    from .utils import parse_jalali_date, auto_assign_paint_tasks

    if request.method != 'POST' or request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})

    date_str = request.POST.get('date')
    if not date_str:
        return JsonResponse({'success': False, 'error': 'تاریخ ارسال نشده'})

    try:
        target_date = parse_jalali_date(date_str)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)})

    gregorian = target_date.togregorian()

    tasks = ProductionTask.objects.filter(
        station_name='paint',
        scheduled_start__date=gregorian,
        status__in=['pending', 'waiting'],
    )
    unassigned_count = tasks.count()
    tasks.update(assigned_worker=None, scheduled_start=None, scheduled_end=None)

    try:
        assigned_count = auto_assign_paint_tasks(target_date=target_date)
    except Exception as exc:
        logger.error(f"خطا در painting_reset_schedule: {exc}\n{traceback.format_exc()}")
        return JsonResponse({'success': False, 'error': f'خطا در بازنشانی: {str(exc)}'})

    remaining_unassigned = ProductionTask.objects.filter(
        station_name='paint',
        scheduled_start__date=gregorian,
        status__in=['pending', 'waiting'],
        assigned_worker__isnull=True,
    ).count()

    message = (
        f'زمان‌بندی بازنشانی شد. '
        f'{unassigned_count} تسک آزاد، {assigned_count} تسک مجدداً تخصیص داده شد. '
        f'{remaining_unassigned} تسک به دلیل نبود ظرفیت یا عدم تأیید skill باقی ماند.'
    )
    return JsonResponse({
        'success': True,
        'message': message,
        'unassigned_count': unassigned_count,
        'assigned_count': assigned_count,
        'remaining_unassigned': remaining_unassigned,
    })


@login_required
@admin_or_manager_required
def painting_repaint_items(request):
    """بازنشانی و زمان‌بندی مجدد تسک‌های نقاشی آیتم‌های انتخاب‌شده (AJAX)"""
    from .utils import parse_jalali_date, repaint_item_ids_for_date

    if request.method != 'POST' or request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'درخواست نامعتبر'})

    item_ids = request.POST.getlist('item_ids[]') or request.POST.getlist('item_ids')
    date_str = request.POST.get('date')

    if not item_ids:
        return JsonResponse({'success': False, 'error': 'هیچ آیتمی انتخاب نشده است'})

    if not date_str:
        return JsonResponse({'success': False, 'error': 'تاریخ ارسال نشده'})

    try:
        target_date = parse_jalali_date(date_str)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)})

    try:
        result = repaint_item_ids_for_date(item_ids, target_date)
        return JsonResponse({
            'success': True,
            'message': f'برنامه‌ریزی مجدد انجام شد. {result.get("scheduled_count", 0)} تسک زمان‌بندی شد.',
            'result': result,
            'redirect': reverse('painting_schedule') + f'?date={target_date.strftime("%Y-%m-%d")}',
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@admin_or_manager_required
def painting_assignment_rules_view(request):
    """مدیریت قوانین تخصیص دستی کارگران به تسک‌های نقاشی بر اساس رنگ و مرحله"""
    from .models import PaintingAssignmentRule
    from .utils import painting_nav_context

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        action = request.POST.get('action')

        if action == 'create':
            worker_id = request.POST.get('worker')
            stage_id = request.POST.get('stage') or None
            process_id = request.POST.get('process') or None
            color_codes_str = request.POST.get('color_codes', '')
            rule_type = request.POST.get('rule_type', 'priority')
            is_active = request.POST.get('is_active', 'true') == 'true'

            if not worker_id:
                return JsonResponse({'success': False, 'error': 'کارگر الزامی است'})

            worker = get_object_or_404(WorkerProfile, pk=worker_id, stage='paint')
            stage = get_object_or_404(PaintingStage, pk=stage_id) if stage_id else None
            process = get_object_or_404(PaintingProcess, pk=process_id) if process_id else None

            color_codes = [c.strip() for c in color_codes_str.split(',') if c.strip()] if color_codes_str else None

            rule = PaintingAssignmentRule.objects.create(
                worker=worker,
                painting_stage=stage,
                process=process,
                color_codes=color_codes,
                rule_type=rule_type,
                priority=100,
                is_active=is_active,
            )
            return JsonResponse({'success': True, 'id': rule.id})

        elif action == 'edit':
            rule_id = request.POST.get('rule_id')
            rule = get_object_or_404(PaintingAssignmentRule, pk=rule_id)

            worker_id = request.POST.get('worker')
            stage_id = request.POST.get('stage') or None
            process_id = request.POST.get('process') or None
            color_codes_str = request.POST.get('color_codes', '')
            rule_type = request.POST.get('rule_type', rule.rule_type)
            is_active = request.POST.get('is_active', 'true') == 'true'

            if worker_id:
                rule.worker = get_object_or_404(WorkerProfile, pk=worker_id, stage='paint')
            if stage_id:
                rule.painting_stage = get_object_or_404(PaintingStage, pk=stage_id)
            else:
                rule.painting_stage = None
            if process_id:
                rule.process = get_object_or_404(PaintingProcess, pk=process_id)
            else:
                rule.process = None

            rule.color_codes = [c.strip() for c in color_codes_str.split(',') if c.strip()] if color_codes_str else None
            rule.rule_type = rule_type
            rule.is_active = is_active
            rule.save()
            return JsonResponse({'success': True})

        elif action == 'delete':
            rule_id = request.POST.get('rule_id')
            rule = get_object_or_404(PaintingAssignmentRule, pk=rule_id)
            rule.delete()
            return JsonResponse({'success': True})

        elif action == 'toggle_active':
            rule_id = request.POST.get('rule_id')
            rule = get_object_or_404(PaintingAssignmentRule, pk=rule_id)
            rule.is_active = not rule.is_active
            rule.save()
            return JsonResponse({'success': True, 'is_active': rule.is_active})

        return JsonResponse({'success': False, 'error': 'عملیات نامعتبر'})

    rules = PaintingAssignmentRule.objects.select_related(
        'worker__user', 'painting_stage', 'painting_stage__process', 'process'
    ).order_by('-priority')

    context = {
        'active_tab': 'assignment_rules',
        'rules': rules,
        'workers': WorkerProfile.objects.filter(stage='paint', is_available=True).select_related('user'),
        'stages': PaintingStage.objects.all().select_related('process'),
        'processes': PaintingProcess.objects.filter(is_active=True),
        **painting_nav_context(),
    }
    return render(request, 'painting_management/assignment_rules.html', context)


@login_required
@admin_or_manager_required
def delete_all_tasks(request, order_id):
    """حذف تمام تسک‌های تولید شده برای یک سفارش و برگرداندن وضعیت به draft"""
    order = get_object_or_404(Order, pk=order_id)

    done_tasks = order.tasks.filter(status='done')
    if done_tasks.exists():
        messages.warning(request, f"{done_tasks.count()} تسک قبلاً تکمیل شده‌اند. با حذف آن‌ها، سابقه از بین می‌رود.")

    with transaction.atomic():
        order.tasks.all().delete()
        order.status = 'draft'
        order.save(update_fields=['status'])

    messages.success(request, f"✅ تمام تسک‌های سفارش {order.id} حذف شدند و وضعیت به پیش‌نویس برگردانده شد.")
    return redirect('order_detail', order_id=order.id)


@login_required
@admin_or_manager_required
def delete_paint_tasks(request, item_id):
    """حذف تمام تسک‌های نقاشی یک آیتم سفارش و پاک کردن زمان‌بندی/تخصیص آن‌ها"""
    item = get_object_or_404(OrderItem, pk=item_id)

    paint_tasks = item.paint_tasks.all()
    count = paint_tasks.count()
    if count == 0:
        messages.warning(request, "هیچ تسک نقاشی‌ای برای این آیتم وجود ندارد.")
        return redirect('item_detail', pk=item.id)

    done_tasks = paint_tasks.filter(status='done')
    if done_tasks.exists():
        messages.warning(request, f"{done_tasks.count()} تسک نقاشی قبلاً تکمیل شده‌اند. با حذف آن‌ها، سابقه از بین می‌رود.")

    with transaction.atomic():
        paint_tasks.delete()

    messages.success(request, f"✅ {count} تسک نقاشی آیتم {item.id} حذف شدند.")
    return redirect('item_detail', pk=item.id)


@login_required
@admin_or_manager_required
def delete_all_paint_tasks_for_order(request, order_id):
    """حذف تمام تسک‌های نقاشی یک سفارش (همه آیتم‌ها) — سایر تسک‌ها حفظ می‌شوند"""
    order = get_object_or_404(Order, pk=order_id)

    paint_tasks = order.tasks.filter(station_name='paint')
    count = paint_tasks.count()
    if count == 0:
        messages.warning(request, "هیچ تسک نقاشی‌ای برای این سفارش وجود ندارد.")
        return redirect('order_detail', order_id=order.id)

    done_tasks = paint_tasks.filter(status='done')
    if done_tasks.exists():
        messages.warning(request, f"{done_tasks.count()} تسک نقاشی قبلاً تکمیل شده‌اند. با حذف آن‌ها، سابقه از بین می‌رود.")

    with transaction.atomic():
        paint_tasks.delete()
        remaining_tasks = order.tasks.exclude(station_name='paint')
        if not remaining_tasks.exists():
            order.status = 'draft'
            order.save(update_fields=['status'])

    messages.success(request, f"✅ {count} تسک نقاشی سفارش {order.id} حذف شدند.")
    return redirect('order_detail', order_id=order.id)



# views.py - در بخش painting management

@login_required
@admin_or_manager_required
def painting_holidays_view(request):
    """مدیریت تعطیلات رسمی"""
    from .utils import painting_nav_context
    from datetime import datetime
    import jdatetime

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        action = request.POST.get('action')

        if action == 'create':
            date_str = request.POST.get('date')  # تاریخ جلالی به فرمت YYYY-MM-DD
            description = request.POST.get('description', '')
            if not date_str:
                return JsonResponse({'success': False, 'error': 'تاریخ الزامی است'})
            try:
                y, m, d = map(int, date_str.split('-'))
                jalali_date = jdatetime.date(y, m, d)
                gregorian_date = jalali_date.togregorian()
                holiday, created = Holiday.objects.get_or_create(
                    date=gregorian_date,
                    defaults={'description': description}
                )
                if not created:
                    return JsonResponse({'success': False, 'error': 'این تاریخ قبلاً ثبت شده است'})
                return JsonResponse({
                    'success': True,
                    'id': holiday.id,
                    'date': jalali_date.strftime('%Y-%m-%d'),
                    'description': description
                })
            except (ValueError, TypeError):
                return JsonResponse({'success': False, 'error': 'فرمت تاریخ نامعتبر'})

        elif action == 'delete':
            holiday_id = request.POST.get('holiday_id')
            holiday = get_object_or_404(Holiday, pk=holiday_id)
            holiday.delete()
            return JsonResponse({'success': True})

    # GET: نمایش لیست
    holidays = Holiday.objects.all().order_by('-date')
    # تبدیل تاریخ‌ها به جلالی برای نمایش
    holidays_jalali = []
    for h in holidays:
        jalali_date = jdatetime.date.fromgregorian(date=h.date)
        holidays_jalali.append({
            'id': h.id,
            'jalali_date': jalali_date.strftime('%Y-%m-%d'),
            'description': h.description,
        })

    context = {
        'active_tab': 'holidays',
        'holidays': holidays_jalali,
        **painting_nav_context(),
    }
    return render(request, 'painting_management/holidays.html', context)
