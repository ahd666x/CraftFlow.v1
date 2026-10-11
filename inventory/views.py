"""
ویوهای پنل انبار.

روند کاربر در این پنل سه قدم است و هر سه از همین فایل بیرون می‌آید:

    ۱. دریافت کالا      — اسکن بارکد  (``raw_material_receive_scan``)
    ۲. تحویل و بازگشت   — صف مواد روزانه (``daily_material_queue``)
    ۳. بستن روز         — کنترل نهایی   (``daily_closing``)

بقیهٔ صفحات فقط‌خواندنی‌اند: گزارش‌ها (یک صفحه با چند تب) و حسابرسی داده.

هیچ ویویی در این فایل موجودی را مستقیم دستکاری نمی‌کند؛ هر نوشتنی از راه
``inventory.services`` انجام می‌شود تا قاعدهٔ بسته‌بندی و کسر مصرف در یک نقطه
بماند.
"""
import json
import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Q, Count
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render, redirect
from django.db.transaction import atomic
from django.views.decorators.http import require_http_methods, require_POST

from product.decorators import (
    warehouse_or_manager_required,
    warehouse_receive_required,
    warehouse_deliver_required,
    day_close_required,
)

from . import reports
from . import services
from .forms import (
    RawMaterialCategoryForm,
    RawMaterialForm,
)
from .models import (
    DailyMaterialQueue,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
    StockCount,
    StockCountLine,
)
from .services import HandoverError

logger = logging.getLogger(__name__)


# ============================================================
# کمکی‌های مشترک
# ============================================================

def _inventory_context(active_tab='queue'):
    return {'active_tab': active_tab}


def _no_store(response):
    """
    این صفحات دادهٔ لحظه‌ای صف را نشان می‌دهند؛ هیچ‌کدام نباید توسط مرورگر یا
    پراکسی کش شوند وگرنه کاربر نسخهٔ کهنهٔ جدول را می‌بیند و تغییرات
    «اعمال نمی‌شود».
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
    """کارگران فعال با پروفایل کارگری (workerprofile)."""
    return list(
        User.objects.filter(is_active=True, workerprofile__isnull=False)
        .select_related('workerprofile')
        .order_by('first_name', 'last_name', 'username')
    )


def _active_materials():
    return RawMaterial.objects.filter(is_active=True).order_by('category__name', 'name')


def _int(raw):
    try:
        return int(raw) if raw not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _parse_date_param(raw):
    """تاریخ شمسی متنی را به تاریخ میلادی برمی‌گرداند؛ ورودی خراب = None."""
    if not raw:
        return None
    try:
        from product.utils import parse_jalali_date
        return parse_jalali_date(str(raw).strip())
    except (ValueError, TypeError):
        return None


def _jalali_today():
    import jdatetime
    return jdatetime.date.today()


def _jalali_str(value):
    """تاریخ میلادی/شمسی را به رشتهٔ شمسی «۱۴۰۵-۰۷-۱۰» تبدیل می‌کند."""
    import jdatetime
    if value is None:
        return ''
    try:
        return jdatetime.date.fromgregorian(date=value).strftime('%Y-%m-%d')
    except (AttributeError, ValueError, TypeError):
        return str(value)


def _is_xhr(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


# ============================================================
#  ۱) دریافت کالا — اسکن بارکد
# ============================================================

@login_required
@warehouse_receive_required
def raw_material_receive_scan(request):
    """
    تنها راه ورود کالا به انبار.

    انباردار بارکد را اسکن می‌کند و تعداد بسته را می‌زند؛ مقدار از
    ``pack_size`` همان ماده حساب می‌شود. قیمت و تأمین‌کننده عمداً پرسیده
    نمی‌شوند چون در روند روزانهٔ انبار نقشی ندارند.
    """
    if request.method == 'POST':
        barcode = request.POST.get('barcode', '').strip()
        pack_count = _int(request.POST.get('pack_count', 1))
        note = request.POST.get('note', '').strip()

        if not barcode:
            messages.error(request, 'بارکد را اسکن یا تایپ کنید.')
            return redirect('inventory:raw_material_receive_scan')

        if pack_count is None or pack_count < 1 or pack_count > 1000:
            messages.error(request, 'تعداد بسته باید عددی بین ۱ تا ۱۰۰۰ باشد.')
            return redirect('inventory:raw_material_receive_scan')

        raw_material = RawMaterial.objects.filter(barcode=barcode).first()
        if raw_material is None:
            messages.error(
                request,
                f'هیچ ماده‌ای با بارکد «{barcode}» ثبت نشده است. '
                'بارکد را در صفحهٔ «مواد اولیه» برای این ماده ثبت کنید.',
            )
            return redirect('inventory:raw_material_receive_scan')

        try:
            quantity = services.receive_quantity(raw_material, pack_count)
        except HandoverError as exc:
            messages.error(request, str(exc))
            return redirect('inventory:raw_material_receive_scan')

        StockMovement.objects.create(
            raw_material=raw_material,
            movement_type='purchase',
            quantity=quantity,
            note=(note or f'دریافت با اسکن بارکد — {pack_count} بسته')[:255],
            created_by=request.user,
        )
        messages.success(
            request,
            f'«{raw_material.name}» — {pack_count} بسته '
            f'({quantity} {raw_material.get_unit_display()}) به انبار اضافه شد.',
        )
        return redirect('inventory:raw_material_receive_scan')

    context = {
        **_inventory_context('receive'),
        'recent_movements': StockMovement.objects.filter(
            movement_type='purchase',
        ).select_related('raw_material', 'created_by').order_by('-created_at')[:10],
        'no_barcode_materials': RawMaterial.objects.filter(
            is_active=True, barcode__isnull=True,
        ).order_by('name'),
    }
    return render(request, 'inventory/receive.html', context)


# ============================================================
#  ۲) صف مواد روزانه — صفحهٔ اصلی پنل
# ============================================================

@login_required
@warehouse_or_manager_required
def daily_material_queue(request):
    """
    صفحهٔ اصلی انبار: چه کسی امروز چه چیزی لازم دارد.

    ردیف‌ها از برنامهٔ تولید مشتق می‌شوند و انباردار فقط تحویل و بازگشت را
    ثبت می‌کند. برای همین تاریخ کاری همیشه «امروز» است مگر کاربر تاریخ دیگری
    را انتخاب کند.
    """
    # No automatic sync on GET - sync happens on task changes or manual refresh
    # services.sync_queue_for_date(_today_gregorian_from_request(request))

    date_str = request.GET.get('date', '').strip()
    selected = _parse_date_param(date_str)
    if selected is None:
        import jdatetime
        selected = jdatetime.date.today()
    selected_gregorian = selected.togregorian()

    report = services.daily_material_report(
        selected_gregorian,
        worker_id=_int(request.GET.get('worker')),
        material_id=_int(request.GET.get('material')),
        status=(request.GET.get('status', '').strip() or None),
    )

    paginator = Paginator(report['rows'], 25)
    queues = paginator.get_page(request.GET.get('page'))

    # Attach action state to each queue row for template buttons
    queues.object_list = list(queues.object_list)
    for q in queues.object_list:
        q.action = services.daily_queue_action_state(q, with_suggestion=False)

    diagnostics = report.get('diagnostics', {})
    unresolved_materials = diagnostics.get('unresolved_materials', [])
    skipped = diagnostics.get('skipped', [])

    context = {
        **_inventory_context('queue'),
        'queues': queues,
        'summary': report['summary'],
        'total_count': paginator.count,
        'selected_date': selected,
        'selected_gregorian': selected_gregorian,
        'date_str': selected.strftime('%Y-%m-%d'),
        'paint_workers': _paint_workers(),
        'materials': _active_materials(),
        'status_choices': DailyMaterialQueue.STATUS_CHOICES,
        'worker_filter': request.GET.get('worker', '').strip(),
        'material_filter': request.GET.get('material', '').strip(),
        'status_filter': request.GET.get('status', '').strip(),
        'page_version': _page_version(),
        'unresolved_materials': unresolved_materials,
        'skipped_tasks': skipped,
    }
    return _no_store(render(request, 'inventory/queue.html', context))


def _today_gregorian_from_request(request):
    """تاریخ کاری انتخابی کاربر، یا امروز."""
    parsed = _parse_date_param(request.GET.get('date', '').strip())
    if parsed is not None:
        return parsed.togregorian()
    import jdatetime
    return jdatetime.date.today().togregorian()


@login_required
@warehouse_deliver_required
@require_POST
def daily_queue_delivery(request, queue_id):
    """ثبت تحویل یک ردیف صف — تنها نقطهٔ کسر موجودی بابت تولید."""
    if not _is_xhr(request):
        return HttpResponseForbidden()

    try:
        raw_quantity = (request.POST.get('quantity') or '').strip() or None
        queue = services.execute_daily_delivery(
            queue_id=queue_id,
            delivered_by=request.user,
            note=str(request.POST.get('note') or '').strip(),
            quantity=raw_quantity,
        )
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception:
        logger.exception('daily_queue_delivery: unexpected error for queue %s', queue_id)
        return JsonResponse(
            {'success': False, 'error': 'ثبت تحویل انجام نشد. لطفاً دوباره تلاش کنید.'},
            status=500,
        )

    state = services.daily_queue_action_state(queue)
    messages.success(
        request,
        f'«{queue.raw_material.name}» تحویل {queue.worker.get_full_name() or queue.worker.username} '
        f'({queue.delivered_quantity} {queue.raw_material.get_unit_display()}).',
    )
    return JsonResponse({
        'success': True,
        'delivered_quantity': str(queue.delivered_quantity),
        'status': queue.status,
        'status_display': queue.get_status_display(),
        'actual_consumption': str(queue.actual_consumption),
        'suggested_delivery': state['suggested_delivery'],
        'auto_returnable': state['auto_returnable'],
        'can_auto_return': state['can_auto_return'],
    })


@login_required
@warehouse_deliver_required
@require_POST
def daily_queue_return(request, queue_id):
    """ثبت بازگشت پایان روز برای یک ردیف صف.

    اگر ``returned_quantity`` خالی باشد، برگشت خودکار مازاد
    (``delivered - planned``) انجام می‌شود؛ هر تفاوی در مصرف از طریق
    ``recalculate_consumption`` ثبت می‌شود.
    """
    if not _is_xhr(request):
        return HttpResponseForbidden()

    raw_quantity = request.POST.get('returned_quantity')

    try:
        if raw_quantity in (None, ''):
            queue = services.execute_auto_return(
                queue_id=queue_id,
                returned_by=request.user,
                note=str(request.POST.get('note') or '').strip(),
            )
        else:
            queue = services.execute_daily_return(
                queue_id=queue_id,
                returned_by=request.user,
                returned_quantity=raw_quantity,
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

    messages.success(request, f'بازگشت «{queue.raw_material.name}» ثبت شد.')
    return JsonResponse({
        'success': True,
        'returned_quantity': str(queue.returned_quantity),
        'actual_consumption': str(queue.actual_consumption),
        'excess_consumption': str(queue.excess_consumption),
        'status': queue.status,
        'status_display': queue.get_status_display(),
    })


@login_required
@warehouse_deliver_required
@require_POST
def daily_queue_auto_return(request, queue_id):
    """ثبت برگشت خودکار مازاد تحویل برای یک ردیف صف."""
    if not _is_xhr(request):
        return HttpResponseForbidden()

    try:
        queue = services.execute_auto_return(
            queue_id=queue_id,
            returned_by=request.user,
            note=str(request.POST.get('note') or '').strip(),
        )
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception:
        logger.exception('daily_queue_auto_return: unexpected error for queue %s', queue_id)
        return JsonResponse(
            {'success': False, 'error': 'برگشت خودکار انجام نشد. لطفاً دوباره تلاش کنید.'},
            status=500,
        )

    messages.success(request, f'برگشت خودکار «{queue.raw_material.name}» ثبت شد.')
    return JsonResponse({
        'success': True,
        'returned_quantity': str(queue.returned_quantity),
        'actual_consumption': str(queue.actual_consumption),
        'excess_consumption': str(queue.excess_consumption),
        'status': queue.status,
        'status_display': queue.get_status_display(),
    })


@login_required
@warehouse_deliver_required
@require_POST
def daily_queue_cancel(request, queue_id):
    """لغو/کنار گذاشتن یک ردیف صف (فقط برای pending بدون تراکنش)."""
    if not _is_xhr(request):
        return HttpResponseForbidden()

    try:
        queue = services.cancel_daily_queue(
            queue_id=queue_id,
            cancelled_by=request.user,
            note=str(request.POST.get('note') or '').strip(),
        )
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception:
        logger.exception('daily_queue_cancel: unexpected error for queue %s', queue_id)
        return JsonResponse(
            {'success': False, 'error': 'لغو ردیف انجام نشد. لطفاً دوباره تلاش کنید.'},
            status=500,
        )

    messages.success(request, f'ردیف «{queue.raw_material.name}» لغو شد.')
    return JsonResponse({
        'success': True,
        'status': queue.status,
        'status_display': queue.get_status_display(),
    })


@login_required
@warehouse_deliver_required
@require_POST
def daily_queue_batch_auto_return(request):
    """برگشت خودکار انبوه تمام ردیف‌های دارای مازاد برای یک تاریخ."""
    if not _is_xhr(request):
        return HttpResponseForbidden()

    date_str = request.POST.get('date', '').strip()
    parsed = _parse_date_param(date_str)
    if parsed is None:
        return JsonResponse({'success': False, 'error': 'تاریخ معتبر نیست.'}, status=400)

    try:
        processed, total_returned, ids = services.execute_batch_auto_return(
            date=parsed.togregorian(),
            returned_by=request.user,
            note=str(request.POST.get('note') or '').strip(),
        )
    except Exception:
        logger.exception('daily_queue_batch_auto_return: unexpected error for date %s', date_str)
        return JsonResponse(
            {'success': False, 'error': 'برگشت خودکار انبوه انجام نشد.'},
            status=500,
        )

    return JsonResponse({
        'success': True,
        'processed': processed,
        'total_returned': str(total_returned),
        'queue_ids': ids,
    })


@login_required
@warehouse_or_manager_required
def daily_queue_sources(request, queue_id):
    """«این نیاز از کجا آمده» — ردیابی منابع یک ردیف صف."""
    try:
        payload = services.queue_sources(queue_id)
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    queue = payload['queue']
    return JsonResponse({
        'success': True,
        'queue_id': queue.id,
        'worker': payload['worker'],
        'material': payload['material'],
        'planned_quantity': str(payload['planned_quantity']),
        'delivered_quantity': str(payload['delivered_quantity']),
        'returned_quantity': str(payload['returned_quantity']),
        'actual_consumption': str(payload['actual_consumption']),
        'max_returnable': payload['max_returnable'],
        'can_return': payload['can_return'],
        'can_deliver': payload['can_deliver'],
        'suggested_delivery': payload['suggested_delivery'],
        'auto_returnable': payload['auto_returnable'],
        'can_auto_return': payload['can_auto_return'],
        'can_cancel': payload['can_cancel'],
        'status': payload['status'],
        'status_display': payload['status_display'],
        'rows': [
            {
                'kind': row['kind'],
                'kind_display': row['kind_display'],
                'label': row['label'],
                'detail': row.get('detail') or '',
                'quantity': str(row['quantity']),
                'task_label': row.get('task_label') or '',
                'stage_name': row.get('stage_name') or '',
                'process_name': row.get('process_name') or '',
                'color_part': row.get('color_part') or '',
                'order_item': row.get('order_item') or '',
            }
            for row in payload['rows']
        ],
    })


@login_required
@warehouse_or_manager_required
def daily_queue_preview_delivery(request, queue_id):
    """پیش‌نمایش تحویل: چند بسته و چه مقدار فیزیکی از انبار خارج می‌شود."""
    try:
        preview = services.preview_daily_delivery(queue_id)
    except HandoverError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)

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
        'remaining': str(preview['remaining']),
        'open_remainder': str(preview['open_remainder']),
        'from_open': str(preview['from_open']),
        'from_new_pack': str(preview['from_new_pack']),
        'remainder_after': str(preview['remainder_after']),
        'can_add_extra': preview['can_add_extra'],
        'suggested_delivery': str(preview['suggested_delivery']),
        'auto_returnable': str(preview['auto_returnable']),
        'can_auto_return': preview['can_auto_return'],
    })


# ============================================================
#  ۳) کنترل و بستن روز
# ============================================================

@login_required
@warehouse_or_manager_required
def daily_closing(request):
    """
    آخرین قدم روز: سه ایراد را نشان می‌دهد و وقتی هیچ‌کدام نبود اجازهٔ
    ثبت سند «روز بررسی و تأیید شد» را می‌دهد.

    این ثبت هیچ حرکت انباری نمی‌سازد و هیچ مقدار صف را تغییر نمی‌دهد.
    """
    date_str = request.GET.get('date', '').strip()
    parsed = _parse_date_param(date_str)
    if parsed is None:
        import jdatetime
        parsed = jdatetime.date.today()

    selected_gregorian = parsed.togregorian()
    # No automatic sync on GET - sync happens on task changes or manual refresh
    # services.sync_queue_for_date(selected_gregorian)

    control = services.daily_closing_status(
        selected_gregorian,
        worker_id=_int(request.GET.get('worker')),
        material_id=_int(request.GET.get('material')),
    )

    context = {
        **_inventory_context('closing'),
        'control': control,
        'closing': services.get_daily_closing(selected_gregorian),
        'summary': control['summary'],
        'problems': control['problems'],
        'selected_date': parsed,
        'selected_gregorian': selected_gregorian,
        'date_str': parsed.strftime('%Y-%m-%d'),
        'paint_workers': _paint_workers(),
        'materials': _active_materials(),
        'worker_filter': request.GET.get('worker', '').strip(),
        'material_filter': request.GET.get('material', '').strip(),
        'page_version': _page_version(),
    }
    return _no_store(render(request, 'inventory/closing.html', context))


@login_required
@day_close_required
@require_POST
def daily_closing_confirm(request):
    """ثبت سند «روز بررسی و تأیید شد»."""
    parsed = _parse_date_param(request.POST.get('date', '').strip())
    if parsed is None:
        import jdatetime
        parsed = jdatetime.date.today()

    force = request.POST.get('force') == 'true'

    try:
        services.confirm_daily_closing(
            date=parsed.togregorian(),
            closed_by=request.user,
            note=request.POST.get('note', ''),
            force=force,
        )
    except HandoverError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'روز {parsed.strftime("%Y/%m/%d")} بسته شد.')
    return redirect('inventory:daily_closing')


# ============================================================
#  اصلاح دستی موجودی
# ============================================================

@login_required
@warehouse_receive_required
@require_POST
def stock_adjustment(request):
    """
    اصلاح انبار برای شمارش نادرست.

    عمداً فقط اصلاحیه می‌سازد: ثبت «مصرف» دستی از این صفحه ممکن نیست تا
    موجودی هیچ‌وقت دوبار کم نشود. برای تحویل مواد فقط صف روزانه راه legal
    است.
    """
    raw_material = get_object_or_404(
        RawMaterial.objects.filter(is_active=True), pk=request.POST.get('raw_material'))
    try:
        amount = Decimal(str(request.POST.get('quantity', '0')).strip())
    except (InvalidOperation, TypeError, ValueError):
        messages.error(request, 'مقدار اصلاحیه معتبر نیست.')
        return redirect('inventory:reports')

    if amount == 0:
        messages.error(request, 'مقدار اصلاحیه نمی‌تواند صفر باشد.')
        return redirect('inventory:reports')

    # require reason for adjustment
    note = str(request.POST.get('note') or '').strip()
    if not note:
        messages.error(request, 'یادداشت/دلیل اصلاحیه الزامی است.')
        return redirect('inventory:reports')

    # Validate adjust_out won't make stock negative
    if amount < 0:
        if raw_material.current_stock + amount < 0:
            messages.error(
                request,
                f'موجودی «{raw_material.name}» ({raw_material.current_stock}) '
                f'برای اصلاح کاهشی {abs(amount)} کافی نیست.'
            )
            return redirect('inventory:reports')

    try:
        services.create_stock_adjustment(
            raw_material=raw_material,
            quantity=amount,
            created_by=request.user,
            note=note,
        )
    except HandoverError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'موجودی «{raw_material.name}» اصلاح شد.',
        )
    # Safe redirect: only allow internal URLs
    next_url = request.POST.get('next', '')
    if next_url and next_url.startswith('/'):
        return redirect(next_url)
    return redirect('inventory:reports')


# ============================================================
#  گزارش‌ها — یک صفحه با چند تب
# ============================================================

REPORT_TABS = (
    ('overview', 'نگاه کلی'),
    ('ledger', 'دفتر گردش انبار'),
    ('consumption', 'تحلیل مصرف'),
    ('history', 'گزارش‌های تاریخی'),
)


@login_required
@warehouse_or_manager_required
def reports_view(request):
    """
    همهٔ گزارش‌های فقط‌خواندنی در یک صفحه.

    به‌جای هفت صفحهٔ جدا که کاربر باید بینشان حدس بزند، چهار تب با فیلترهای
    مشترک. هیچ‌چیز اینجا نوشته نمی‌شود، مگر فرم «اصلاح موجودی».
    """
    tab = (request.GET.get('tab') or 'overview').strip()
    if tab not in dict(REPORT_TABS):
        tab = 'overview'

    filters = _report_filters(request)
    context = {
        **_inventory_context('reports'),
        'tabs': REPORT_TABS,
        'tab': tab,
        'filters': filters,
        'materials': _active_materials(),
        'paint_workers': _paint_workers(),
        'page_version': _page_version(),
    }

    if tab == 'ledger':
        context['ledger'] = reports.material_ledger(**filters)
    elif tab == 'consumption':
        context['consumption'] = reports.consumption_report(
            date_from=filters.get('date_from'),
            date_to=filters.get('date_to'),
            material_id=filters.get('material_id'),
            worker_id=filters.get('worker_id'),
        )
    elif tab == 'history':
        context['history'] = reports.historical_report(
            report=(request.GET.get('report') or 'consumption').strip(),
            date_from=filters.get('date_from'),
            date_to=filters.get('date_to'),
            material_id=filters.get('material_id'),
            worker_id=filters.get('worker_id'),
            order_id=filters.get('order_id'),
            product_id=_int(request.GET.get('product')),
            color_part=(request.GET.get('color_part') or '').strip() or None,
            status=(request.GET.get('status') or '').strip() or None,
        )
        context['history_report_key'] = request.GET.get('report') or 'consumption'
    else:
        context['dashboard'] = reports.material_planning_dashboard(
            date=filters.get('date_to'))
        # inventory_alerts یک dict برمی‌گرداند، نه یک لیست.
        alert_data = reports.inventory_alerts(date=filters.get('date_to'))
        context['alerts'] = alert_data['alerts']
        context['alert_total'] = alert_data['total']
        context['low_stock'] = reports.low_stock_rows()

    return render(request, 'inventory/reports.html', context)


def _report_filters(request):
    """فیلترهای مشترک گزارش‌ها، خوانا و بدون تکرار."""
    date_from = _parse_date_param(request.GET.get('date_from'))
    date_to = _parse_date_param(request.GET.get('date_to'))
    return {
        'date_from': date_from.togregorian() if date_from else None,
        'date_to': date_to.togregorian() if date_to else None,
        'material_id': _int(request.GET.get('material')),
        'worker_id': _int(request.GET.get('worker')),
        'order_id': _int(request.GET.get('order')),
        'movement_type': (request.GET.get('movement_type') or '').strip() or None,
    }


@login_required
@warehouse_or_manager_required
def data_integrity_audit(request):
    """حسابرسی یکپارچگی داده — فقط خواندنی، هیچ چیزی را خودکار اصلاح نمی‌کند."""
    data = reports.data_integrity_audit()
    context = {
        **_inventory_context('audit'),
        **data,
        'page_version': _page_version(),
    }
    return render(request, 'inventory/audit.html', context)


# ============================================================
# شمارش انبار (Stocktake)
# ============================================================

@login_required
@warehouse_or_manager_required
def stock_count_list(request):
    """فهرست سندهای شمارش انبار."""
    qs = StockCount.objects.select_related('created_by', 'approved_by').all()
    context = {
        **_inventory_context('stock_count'),
        'counts': qs,
        'page_version': _page_version(),
    }
    return render(request, 'inventory/stock_count_list.html', context)


@login_required
@warehouse_or_manager_required
def stock_count_create(request):
    """ایجاد سند شمارش انبار جدید."""
    if request.method == 'POST':
        import jdatetime
        from django.db import transaction
        note = request.POST.get('note', '').strip()
        date_str = request.POST.get('date', '').strip()
        parsed = _parse_date_param(date_str)
        if parsed is None:
            messages.error(request, 'تاریخ معتبر نیست.')
            return redirect('inventory:stock_count_create')
        
        count_date = parsed.togregorian()
        
        with transaction.atomic():
            # Create stock count with draft status
            stock_count = StockCount.objects.create(
                date=count_date,
                note=note,
                created_by=request.user,
                status='draft',
            )
            # Pre-populate lines with all active materials and their current stock
            materials = RawMaterial.objects.filter(is_active=True).select_related('category')
            for material in materials:
                StockCountLine.objects.create(
                    stock_count=stock_count,
                    raw_material=material,
                    system_quantity=material.current_stock,
                    counted_quantity=material.current_stock,  # Default to current stock
                )
        
        messages.success(request, f'سند شمارش برای {parsed.strftime("%Y/%m/%d")} ساخته شد.')
        return redirect('inventory:stock_count_detail', count_id=stock_count.pk)
    
    context = {
        **_inventory_context('stock_count'),
        'page_version': _page_version(),
        'today_jalali': jdatetime.date.today(),
    }
    return render(request, 'inventory/stock_count_create.html', context)


@login_required
@warehouse_or_manager_required
def stock_count_detail(request, count_id):
    """نمایش و ویرایش خطوط سند شمارش (فقط در حالت draft)."""
    stock_count = get_object_or_404(StockCount, pk=count_id)
    lines = stock_count.lines.select_related('raw_material', 'raw_material__category').all()
    
    if request.method == 'POST' and stock_count.can_edit():
        # Update counted quantities
        for line in lines:
            key = f'counted_{line.pk}'
            if key in request.POST:
                try:
                    line.counted_quantity = Decimal(request.POST[key] or '0')
                    line.save(update_fields=['counted_quantity'])
                except (ValueError, InvalidOperation):
                    pass
        messages.success(request, 'مقادیر شمرده شده ذخیره شد.')
        return redirect('inventory:stock_count_detail', count_id=stock_count.pk)
    
    context = {
        **_inventory_context('stock_count'),
        'stock_count': stock_count,
        'lines': lines,
        'page_version': _page_version(),
    }
    return render(request, 'inventory/stock_count_detail.html', context)


@login_required
@warehouse_or_manager_required
@require_POST
def stock_count_approve(request, count_id):
    """تأیید سند شمارش و ایجاد حرکات اصلاح برای خطوط با اختلاف."""
    stock_count = get_object_or_404(StockCount, pk=count_id)
    
    if not stock_count.can_edit():
        messages.error(request, 'این سند شمارش قبلاً تأیید شده است.')
        return redirect('inventory:stock_count_detail', count_id=count_id)
    
    try:
        stock_count.approve(request.user)
        messages.success(request, 'سند شمارش تأیید شد و حرکات اصلاح ثبت گردید.')
    except ValueError as e:
        messages.error(request, str(e))
    
    return redirect('inventory:stock_count_detail', count_id=count_id)


@login_required
@warehouse_or_manager_required
@require_POST
def stock_count_reverse_purchase(request):
    """ابطال یک حرکت خرید/ورود."""
    if not _is_xhr(request):
        return HttpResponseForbidden()
    
    movement_id = request.POST.get('movement_id')
    note = request.POST.get('note', '').strip()
    
    if not movement_id:
        return JsonResponse({'success': False, 'error': 'شناسه حرکت مشخص نشده.'}, status=400)
    
    try:
        movement_id = int(movement_id)
    except ValueError:
        return JsonResponse({'success': False, 'error': 'شناسه حرکت معتبر نیست.'}, status=400)
    
    try:
        reversal = services.reverse_purchase(
            movement_id=movement_id,
            reversed_by=request.user,
            note=note,
        )
        return JsonResponse({
            'success': True,
            'message': f'دریافت ابطال شد. موجودی جدید: {reversal.raw_material.current_stock}',
            'new_stock': str(reversal.raw_material.current_stock),
        })
    except services.HandoverError as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)
    except Exception:
        logger.exception('stock_count_reverse_purchase failed')
        return JsonResponse({'success': False, 'error': 'ابطال دریافت انجام نشد.'}, status=500)


@login_required
@warehouse_or_manager_required
def material_ledger_export(request):
    """خروجی CSV/Excel دفتر گردش مواد."""
    import csv
    from django.http import HttpResponse
    
    filters = _report_filters(request)
    export_format = request.GET.get('format', 'csv').lower()
    
    ledger = reports.material_ledger(**filters, export=True)
    
    if export_format == 'xlsx':
        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "دفتر گردش مواد"
            
            # Headers
            headers = ['تاریخ', 'ماده', 'نوع', 'مقدار', 'واحد', 'موجودی', 'یادداشت', 'مرجع']
            ws.append(headers)
            
            for line in ledger['rows']:
                mv = line['movement']
                ws.append([
                    mv.created_at.strftime('%Y-%m-%d %H:%M'),
                    mv.raw_material.name,
                    mv.get_movement_type_display(),
                    line['signed'],
                    mv.raw_material.get_unit_display(),
                    line['balance'],
                    mv.note or '',
                    line['reference'] or '',
                ])
            
            response = HttpResponse(
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            response['Content-Disposition'] = 'attachment; filename="material_ledger.xlsx"'
            wb.save(response)
            return response
        except ImportError:
            # Fallback to CSV if openpyxl not available
            export_format = 'csv'
    
    # CSV export
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="material_ledger.csv"'
    response.write('\ufeff')  # BOM for Excel UTF-8
    
    writer = csv.writer(response)
    writer.writerow(['تاریخ', 'ماده', 'نوع', 'مقدار', 'واحد', 'موجودی', 'یادداشت', 'مرجع'])
    
    for line in ledger['rows']:
        mv = line['movement']
        writer.writerow([
            mv.created_at.strftime('%Y-%m-%d %H:%M'),
            mv.raw_material.name,
            mv.get_movement_type_display(),
            line['signed'],
            mv.raw_material.get_unit_display(),
            line['balance'],
            mv.note or '',
            line['reference'] or '',
        ])
    
    return response


@login_required
@warehouse_or_manager_required
def order_material_traceability(request, order_id):
    """مسیر ماده از سفارش تا تحویل و بازگشت."""
    from product.models import Order

    order = get_object_or_404(Order.objects.select_related('customer'), pk=order_id)
    context = {
        **_inventory_context('reports'),
        'order': order,
        'trace': reports.order_material_traceability(order),
        'page_version': _page_version(),
    }
    return render(request, 'inventory/order_trace.html', context)


# ============================================================
#  تنظیمات: مواد اولیه و دسته‌ها
# ============================================================

@login_required
@warehouse_or_manager_required
def material_list(request):
    """فهرست مواد اولیه + فرم ساخت/ویرایش."""
    search = request.GET.get('q', '').strip()
    category_id = _int(request.GET.get('category'))

    qs = RawMaterial.objects.select_related('category').all()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search)
                       | Q(barcode__icontains=search))
    if category_id:
        qs = qs.filter(category_id=category_id)
    qs = qs.order_by('category__name', 'name')

    paginator = Paginator(qs, 25)
    context = {
        **_inventory_context('materials'),
        'materials': paginator.get_page(request.GET.get('page')),
        'total_count': paginator.count,
        'categories': RawMaterialCategory.objects.all(),
        'search': search,
        'category_filter': request.GET.get('category', '').strip(),
        'form': RawMaterialForm(),
        'page_version': _page_version(),
    }
    return render(request, 'inventory/materials.html', context)


@login_required
@warehouse_or_manager_required
def material_detail_api(request, material_id):
    material = get_object_or_404(
        RawMaterial.objects.select_related('category'), pk=material_id)
    if not _is_xhr(request):
        return HttpResponseForbidden()
    return JsonResponse({
        'success': True,
        'id': material.id,
        'name': material.name,
        'code': material.code,
        'barcode': material.barcode,
        'category': material.category_id,
        'unit': material.unit,
        'min_stock_alert': str(material.min_stock_alert),
        'pack_size': str(material.pack_size or 0),
        'stock': str(material.current_stock),
        'is_active': material.is_active,
    })


@login_required
@warehouse_or_manager_required
@require_http_methods(['POST'])
def material_create(request):
    form = RawMaterialForm(request.POST)
    if form.is_valid():
        material = form.save()
        messages.success(request, f'ماده «{material.name}» ثبت شد.')
        if _is_xhr(request):
            return JsonResponse({'success': True, 'redirect': 'inventory:material_list'})
        return redirect('inventory:material_list')
    if _is_xhr(request):
        return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    # For non-AJAX, re-render with errors
    qs = RawMaterial.objects.select_related('category').all()
    paginator = Paginator(qs, 25)
    context = {
        **_inventory_context('materials'),
        'materials': paginator.get_page(request.GET.get('page')),
        'total_count': paginator.count,
        'categories': RawMaterialCategory.objects.all(),
        'form': form,
        'page_version': _page_version(),
    }
    return render(request, 'inventory/materials.html', context)


@login_required
@warehouse_or_manager_required
def material_edit(request, material_id):
    material = get_object_or_404(RawMaterial, pk=material_id)
    if request.method == 'POST':
        form = RawMaterialForm(request.POST, instance=material)
        if form.is_valid():
            material = form.save()
            messages.success(request, f'ماده «{material.name}» ویرایش شد.')
            if _is_xhr(request):
                return JsonResponse({'success': True, 'redirect': 'inventory:material_list'})
            return redirect('inventory:material_list')
        if _is_xhr(request):
            return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    else:
        form = RawMaterialForm(instance=material)
    
    qs = RawMaterial.objects.select_related('category').all()
    paginator = Paginator(qs, 25)
    context = {
        **_inventory_context('materials'),
        'materials': paginator.get_page(request.GET.get('page')),
        'total_count': paginator.count,
        'categories': RawMaterialCategory.objects.all(),
        'form': form,
        'page_version': _page_version(),
    }
    return render(request, 'inventory/materials.html', context)


@login_required
@warehouse_or_manager_required
@require_http_methods(['POST'])
def material_delete(request, material_id):
    material = get_object_or_404(RawMaterial, pk=material_id)
    name = material.name
    material.delete()
    messages.success(request, f'ماده «{name}» حذف شد.')
    if _is_xhr(request):
        return JsonResponse({'success': True})
    return redirect('inventory:material_list')


@login_required
@warehouse_or_manager_required
def category_list(request):
    """فهرست دسته‌های مواد + فرم ساخت."""
    qs = RawMaterialCategory.objects.annotate(
        material_count=Count('materials'),
    ).order_by('name')
    context = {
        **_inventory_context('categories'),
        'categories': qs,
        'form': RawMaterialCategoryForm(),
        'page_version': _page_version(),
    }
    return render(request, 'inventory/categories.html', context)


@login_required
@warehouse_or_manager_required
def category_detail_api(request, category_id):
    category = get_object_or_404(RawMaterialCategory, pk=category_id)
    if not _is_xhr(request):
        return HttpResponseForbidden()
    return JsonResponse({
        'success': True,
        'id': category.id,
        'name': category.name,
    })


@login_required
@warehouse_or_manager_required
@require_http_methods(['POST'])
def category_create(request):
    form = RawMaterialCategoryForm(request.POST)
    if form.is_valid():
        category = form.save()
        messages.success(request, f'دسته «{category.name}» ثبت شد.')
        if _is_xhr(request):
            return JsonResponse({'success': True, 'redirect': 'inventory:category_list'})
        return redirect('inventory:category_list')
    if _is_xhr(request):
        return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    qs = RawMaterialCategory.objects.annotate(material_count=Count('materials')).order_by('name')
    context = {
        **_inventory_context('categories'),
        'categories': qs,
        'form': form,
        'page_version': _page_version(),
    }
    return render(request, 'inventory/categories.html', context)


@login_required
@warehouse_or_manager_required
@require_http_methods(['POST'])
def category_edit(request, category_id):
    category = get_object_or_404(RawMaterialCategory, pk=category_id)
    form = RawMaterialCategoryForm(request.POST, instance=category)
    if form.is_valid():
        form.save()
        messages.success(request, f'دسته «{category.name}» ویرایش شد.')
        if _is_xhr(request):
            return JsonResponse({'success': True, 'redirect': 'inventory:category_list'})
        return redirect('inventory:category_list')
    if _is_xhr(request):
        return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    qs = RawMaterialCategory.objects.annotate(material_count=Count('materials')).order_by('name')
    context = {
        **_inventory_context('categories'),
        'categories': qs,
        'form': form,
        'page_version': _page_version(),
    }
    return render(request, 'inventory/categories.html', context)


@login_required
@warehouse_or_manager_required
@require_http_methods(['POST'])
def category_delete(request, category_id):
    category = get_object_or_404(RawMaterialCategory, pk=category_id)
    if category.materials.exists():
        messages.error(
            request,
            f'دسته «{category.name}» خالی نیست؛ ابتدا موادش را به دستهٔ دیگری '
            'منتقل کنید.',
        )
        if _is_xhr(request):
            return JsonResponse({'success': False, 'error': 'دسته خالی نیست.'}, status=400)
        return redirect('inventory:category_list')
    name = category.name
    category.delete()
    messages.success(request, f'دسته «{name}» حذف شد.')
    if _is_xhr(request):
        return JsonResponse({'success': True})
    return redirect('inventory:category_list')


@login_required
@warehouse_or_manager_required
@require_POST
def daily_queue_refresh(request):
    """بازسازی دستی صف برای یک تاریخ (دکمهٔ Refresh در UI)."""
    if not _is_xhr(request):
        return HttpResponseForbidden()

    date_str = request.POST.get('date', '').strip()
    parsed = _parse_date_param(date_str)
    if parsed is None:
        return JsonResponse({'success': False, 'error': 'تاریخ معتبر نیست.'}, status=400)

    try:
        queue_count, diagnostics = services.sync_queue_for_date(parsed.togregorian())
    except Exception:
        logger.exception('daily_queue_refresh: failed for date %s', date_str)
        return JsonResponse(
            {'success': False, 'error': 'بازسازی صف انجام نشد.'},
            status=500,
        )

    return JsonResponse({
        'success': True,
        'queue_count': queue_count,
        'unresolved_materials': diagnostics.get('unresolved_materials', []),
        'skipped_tasks': diagnostics.get('skipped', []),
        'message': f'{queue_count} ردیف صف ساخته/به‌روزرسانی شد.',
    })


@login_required
@warehouse_or_manager_required
@require_POST
def daily_queue_repair(request):
    """تعمیر اجباری صف: حذف کامل و بازسازی برای یک تاریخ."""
    if not _is_xhr(request):
        return HttpResponseForbidden()

    date_str = request.POST.get('date', '').strip()
    parsed = _parse_date_param(date_str)
    if parsed is None:
        return JsonResponse({'success': False, 'error': 'تاریخ معتبر نیست.'}, status=400)

    try:
        with transaction.atomic():
            # Delete all queues for this date
            deleted_count, _ = DailyMaterialQueue.objects.filter(
                work_date=parsed.togregorian()
            ).delete()
            # Force rebuild
            queue_count, diagnostics = services.sync_queue_for_date(parsed.togregorian())
    except Exception:
        logger.exception('daily_queue_repair: failed for date %s', date_str)
        return JsonResponse(
            {'success': False, 'error': 'تعمیر صف انجام نشد.'},
            status=500,
        )

    return JsonResponse({
        'success': True,
        'deleted_count': deleted_count,
        'queue_count': queue_count,
        'unresolved_materials': diagnostics.get('unresolved_materials', []),
        'skipped_tasks': diagnostics.get('skipped', []),
        'message': f'{deleted_count} ردیف حذف و {queue_count} ردیف مجدداً ساخته شد.',
    })