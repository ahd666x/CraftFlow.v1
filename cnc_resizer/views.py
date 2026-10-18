from django.http import JsonResponse
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.http import Http404
from django.db import transaction
from django.utils import timezone

from . import core
from .models import CncFile, CncPartProfile, CncPlanningIssue, CncSheetPlan
from product.models import Part
from .services import _base_record_for_part


def cnc_operator_required(view_func):
    """Allow managers and the CNC worker who opened a part from scanning."""
    from functools import wraps
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        user = request.user
        from cutting.permissions import can_operate
        allowed = can_operate(user)
        try:
            allowed = allowed or user.workerprofile.stage == 'cnc'
        except Exception:
            pass
        if not allowed:
            if view_func.__name__ == 'index':
                raise PermissionDenied
            return JsonResponse({'success': False, 'error': 'دسترسی به ابزار CNC مجاز نیست.'}, status=403)
        return view_func(request, *args, **kwargs)
    return wrapped


@login_required
@cnc_operator_required
@require_GET
def index(request):
    return render(request, "cnc_resizer/index.html", {
        "api_url": reverse("cnc_resizer:process"),
        "analyze_api_url": reverse("cnc_resizer:analyze"),
        "archive_api_url": reverse("cnc_resizer:archive_files"),
        "profile_api_url": reverse("cnc_resizer:part_profile"),
        "sheet_plans_url": reverse("cnc_resizer:sheet_plans"),
        "part_id": request.GET.get("part_id", ""),
    })


@login_required
@cnc_operator_required
@require_POST
def process(request):
    """JSON in  : {filename, content (the .cnc text), params}
       JSON out : see core.process().  CSRF is enforced as usual (the page sends the token)."""
    status, data = core.handle_request(request.body)
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


@login_required
@cnc_operator_required
@require_POST
def analyze(request):
    """Inventory G-code features for operator profile authoring."""
    import json
    try:
        payload = json.loads(request.body.decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('بدنهٔ درخواست باید یک شیء JSON باشد.')
        params = payload.get('params') or {}
        if not isinstance(params, dict):
            raise ValueError('پارامترهای تحلیل باید یک شیء باشند.')
        result = core.analyze(payload['content'], params)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, core.CncError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400,
                            json_dumps_params={'ensure_ascii': False})
    return JsonResponse(result, json_dumps_params={'ensure_ascii': False})


@login_required
@cnc_operator_required
@require_GET
def archive_files(request):
    record_id = request.GET.get("id")
    if record_id:
        record = CncFile.objects.filter(pk=record_id, kind=CncFile.KIND_BASE).first()
        if not record or not record.path:
            raise Http404("فایل CNC آرشیو پیدا نشد")
        try:
            with open(record.path, "r", encoding="utf-8-sig") as stream:
                content = stream.read(core.MAX_BYTES + 1)
        except OSError:
            raise Http404("خواندن فایل CNC ممکن نیست")
        return JsonResponse({
            "id": record.id,
            "name": record.barcode_key,
            "resizable": record.resizable,
            "content": content,
        }, json_dumps_params={"ensure_ascii": False})

    records = CncFile.objects.filter(
        kind=CncFile.KIND_BASE
    ).values("id", "barcode_key", "length_before", "resizable", "created_at")
    return JsonResponse({
        "files": [
            {
                "id": row["id"],
                "name": row["barcode_key"],
                "length": row["length_before"],
                "resizable": row["resizable"],
            }
            for row in records
        ]
    }, json_dumps_params={"ensure_ascii": False})


@login_required
@cnc_operator_required
@require_http_methods(['GET', 'POST'])
def part_profile(request):
    part_id = request.GET.get('part_id') or request.POST.get('part_id')
    part = Part.objects.filter(pk=part_id).select_related('base_part').first()
    if not part:
        return JsonResponse({'success': False, 'error': 'قطعه پیدا نشد.'}, status=404)
    if request.method == 'GET':
        try:
            profile = part.cnc_profile
            params = profile.params
            approved = profile.approved
            source_id = profile.source_file_id
            feature_profile = profile.feature_profile
            feature_source_id = profile.feature_source_file_id
        except CncPartProfile.DoesNotExist:
            params, approved, source_id = {'mode': 'equal', 'symmetric': True, 'axis': 'length'}, False, None
            feature_profile, feature_source_id = {}, None
        try:
            base = part.base_part or part
            source = CncFile.objects.filter(pk=feature_source_id, kind=CncFile.KIND_BASE).first() if feature_source_id else None
            source = source or _base_record_for_part(base, request.user)
            with open(source.path, 'r', encoding='utf-8-sig') as stream:
                content = stream.read(core.MAX_BYTES + 1)
        except Exception as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=404)
        base = part.base_part or part
        return JsonResponse({'success': True, 'part_id': part.id, 'part_name': part.name,
                             'source_id': source_id or source.id, 'source_name': source.barcode_key,
                             'content': content, 'params': params, 'approved': approved,
                             'feature_profile': feature_profile, 'feature_source_id': feature_source_id,
                             'part_length': float(part.length), 'base_length': float(base.length)})
    import json
    try:
        payload = json.loads(request.body.decode('utf-8'))
        source_id = int(payload['source_id'])
        params = payload.get('params') or {}
        if not isinstance(params, dict):
            raise ValueError('پارامترهای الگو باید یک شیء باشند.')
        if params.get('axis', 'length') != 'length':
            raise ValueError('محور CNC باید length باشد؛ Y فایل CNC برابر Part.length است.')
        source = CncFile.objects.get(pk=source_id, kind=CncFile.KIND_BASE, resizable=True)
        feature_profile = payload.get('feature_profile')
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, CncFile.DoesNotExist) as exc:
        return JsonResponse({'success': False, 'error': str(exc) or 'اطلاعات الگو معتبر نیست.'}, status=400)
    if feature_profile is not None:
        try:
            with open(source.path, 'r', encoding='utf-8-sig') as stream:
                source_text = stream.read(core.MAX_BYTES + 1)
            validation = core.analyze(source_text, {'profile': feature_profile})
        except (OSError, core.CncError, TypeError, ValueError) as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400,
                                json_dumps_params={'ensure_ascii': False})
        if not validation.get('ready'):
            return JsonResponse({'success': False, 'error': 'پروفایل ویژگی‌ها کامل یا معتبر نیست.',
                                 'profile_errors': validation.get('profile_errors', [])}, status=400,
                                json_dumps_params={'ensure_ascii': False})
        profile, _ = CncPartProfile.objects.get_or_create(
            part=part,
            defaults={'source_file': source, 'params': {'mode': 'equal', 'symmetric': True, 'axis': 'length'}, 'approved': False},
        )
        profile.feature_profile = feature_profile
        profile.feature_source_file = source
        profile.feature_profile_updated_by = request.user
        profile.feature_profile_updated_at = timezone.now()
        profile.save(update_fields=('feature_profile', 'feature_source_file', 'feature_profile_updated_by', 'feature_profile_updated_at', 'updated_at'))
        return JsonResponse({'success': True, 'feature_profile_saved': True,
                             'message': 'تحلیل ویژگی‌ها ذخیره شد؛ این پروفایل هنوز در تولید G-code استفاده نمی‌شود.'},
                            json_dumps_params={'ensure_ascii': False})
    profile, _ = CncPartProfile.objects.update_or_create(
        part=part,
        defaults={'source_file': source, 'params': params, 'approved': True, 'approved_by': request.user},
    )
    if part.base_part_id:
        CncPartProfile.objects.update_or_create(
            part=part.base_part,
            defaults={'source_file': source, 'params': params, 'approved': True, 'approved_by': request.user},
        )
    return JsonResponse({'success': True, 'profile_id': profile.id, 'message': 'الگوی CNC برای این قطعه ثبت و تأیید شد.'})


def _layout_preview(plan):
    """Percent-based rectangles so the template can draw the sheet without JS."""
    sheet_w, sheet_h = float(plan.width), float(plan.height)
    boxes = []
    for piece in (plan.layout or {}).get('parts', []):
        try:
            x, y = float(piece['x']) + 5, float(piece['y']) + 5
            w, h = float(piece['width']), float(piece['height'])
        except (KeyError, TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        boxes.append({
            'left': round(x / sheet_w * 100, 3), 'width': round(w / sheet_w * 100, 3),
            'top': round((sheet_h - y - h) / sheet_h * 100, 3), 'height': round(h / sheet_h * 100, 3),
            'label': piece.get('f3') or piece.get('label') or '', 'rotated': piece.get('rotated', False),
        })
    return boxes


@login_required
@cnc_operator_required
@require_GET
def sheet_plans(request):
    from cutting.permissions import can_manage
    can_view_all = request.user.is_staff or request.user.is_superuser or can_manage(request.user)
    plan_query = CncSheetPlan.objects.all() if can_view_all else CncSheetPlan.objects.filter(created_by=request.user)
    plans = list(plan_query.select_related('material', 'source_stock')[:100])
    for plan in plans:
        plan.preview = _layout_preview(plan)
        plan.orders = sorted({p.get('order_id') for p in (plan.layout or {}).get('parts', []) if p.get('order_id')})
        plan.aspect = round(float(plan.height) / float(plan.width), 4) if plan.width else 1
    issue_query = CncPlanningIssue.objects.all() if can_view_all else CncPlanningIssue.objects.filter(created_by=request.user)
    issues = issue_query.select_related('task', 'order', 'part', 'resolved_by')[:100]
    active_count = sum(plan.status == 'planned' for plan in plans)
    return render(request, 'cnc_resizer/sheet_plans.html', {'plans': plans, 'issues': issues, 'active_count': active_count})


@login_required
@cnc_operator_required
@require_POST
def cancel_sheet_plan(request, plan_id):
    """Release a planned sheet so its stock and its parts can be planned again."""
    from django.contrib import messages
    from django.shortcuts import get_object_or_404, redirect
    with transaction.atomic():
        plan = get_object_or_404(CncSheetPlan.objects.select_for_update(), pk=plan_id, status='planned')
        if not _may_access_sheet_plan(request.user, plan):
            raise PermissionDenied
        plan.status = 'cancelled'
        plan.save(update_fields=['status'])
    messages.success(request, f'برنامه ورق #{plan.pk} لغو شد و ورق و قطعات آن آزاد شدند.')
    return redirect('cnc_resizer:sheet_plans')


@login_required
@cnc_operator_required
@require_POST
def complete_sheet_plan(request, plan_id):
    """Record CNC sheet outcome, consume inventory if used/scrapped, and complete cut tasks if used."""
    from collections import Counter
    from decimal import Decimal, InvalidOperation
    from django.contrib import messages
    from django.shortcuts import get_object_or_404, redirect
    from cutting.models import SheetStock, SheetStockEvent
    from product.models import ProductionTask

    try:
        with transaction.atomic():
            plan = get_object_or_404(
                CncSheetPlan.objects.select_for_update().select_related('material'),
                pk=plan_id, status='planned',
            )
            if not _may_access_sheet_plan(request.user, plan):
                raise PermissionDenied
            outcome = request.POST.get('outcome', '')
            note = request.POST.get('note', '').strip()[:255]
            if outcome not in ('used', 'untouched', 'scrapped'):
                raise ValueError('نتیجهٔ ورق معتبر نیست.')
            if outcome == 'scrapped' and not note:
                raise ValueError('برای اسقاط ورق، ثبت علت الزامی است.')

            remnants = []
            if outcome == 'used':
                for index in range(1, 51):
                    raw_width = request.POST.get(f'rem_{plan.pk}_{index}_width', '').strip()
                    raw_height = request.POST.get(f'rem_{plan.pk}_{index}_height', '').strip()
                    if not raw_width and not raw_height:
                        continue
                    if not raw_width or not raw_height:
                        raise ValueError('برای هر پرت واقعی باید هم عرض و هم طول وارد شود.')
                    try:
                        width, height = Decimal(raw_width), Decimal(raw_height)
                    except (InvalidOperation, TypeError):
                        raise ValueError('ابعاد پرت باید عددی باشند.')
                    if not width.is_finite() or not height.is_finite() or width <= 0 or height <= 0:
                        raise ValueError('ابعاد پرت باید بزرگ‌تر از صفر باشند.')
                    if width > plan.width or height > plan.height:
                        raise ValueError('ابعاد پرت نمی‌تواند از ابعاد ورق بیشتر باشد.')
                    if width.quantize(Decimal('0.1')) != width or height.quantize(Decimal('0.1')) != height:
                        raise ValueError('ابعاد پرت حداکثر یک رقم اعشار می‌پذیرند.')
                    remnants.append({'width': format(width, 'f'), 'height': format(height, 'f')})
                if sum(Decimal(r['width']) * Decimal(r['height']) for r in remnants) > plan.width * plan.height:
                    raise ValueError('مجموع مساحت پرت‌ها از مساحت ورق مبدأ بیشتر است.')

            source = get_object_or_404(SheetStock.objects.select_for_update(), pk=plan.source_stock_id)
            if not source.active or source.quantity < 1:
                raise ValueError('ورق رزروشده دیگر در موجودی فعال نیست؛ نتیجه ثبت نشد.')
            if outcome in ('used', 'scrapped'):
                from cutting.views import _reserved_counts
                future_reserved = _reserved_counts([source.pk])[source.pk] - 1
                if source.quantity - 1 < future_reserved:
                    raise ValueError('کسر این ورق باعث کمبود رزرو برنامهٔ دیگری می‌شود.')

            piece_counts = Counter()
            for piece in (plan.layout or {}).get('parts', []):
                if piece.get('task_id') is not None:
                    piece_counts[int(piece['task_id'])] += 1
            if outcome == 'used' and not piece_counts:
                raise ValueError('هیچ تسک معتبری در برنامه پیدا نشد؛ نتیجه و موجودی تغییر نکرد.')
            tasks = {
                task.pk: task for task in ProductionTask.objects.select_for_update()
                .filter(pk__in=piece_counts).order_by('pk')
            }
            if outcome == 'used':
                if set(tasks) != set(piece_counts):
                    raise ValueError('برخی از تسک‌های برنامه دیگر موجود نیستند؛ نتیجه ثبت نشد.')
                for task_id, count in piece_counts.items():
                    task = tasks[task_id]
                    if task.status != 'pending' or task.completed_quantity + count > task.quantity:
                        raise ValueError('تسک‌های این برنامه از زمان برنامه‌ریزی تغییر کرده‌اند؛ نتیجه ثبت نشد.')

            if outcome != 'untouched':
                source.quantity -= 1
                if source.quantity == 0:
                    source.active = False
                    source.inactive_reason = f'مصرف در برنامه CNC #{plan.pk}'
                    source.save(update_fields=('quantity', 'active', 'inactive_reason', 'updated_at'))
                else:
                    source.save(update_fields=('quantity', 'updated_at'))
            event_type = {'used': 'consume', 'untouched': 'untouched', 'scrapped': 'scrap'}[outcome]
            SheetStockEvent.objects.create(
                stock=source, material=plan.material, cnc_plan=plan, event_type=event_type,
                quantity_delta=-1 if outcome != 'untouched' else 0,
                width=plan.width, height=plan.height, is_offcut=False,
                note=note or f'ثبت نتیجه برنامه CNC #{plan.pk}', created_by=request.user,
            )
            if outcome == 'used':
                for remnant in remnants:
                    offcut = SheetStock.objects.create(
                        material=plan.material, width=Decimal(remnant['width']), height=Decimal(remnant['height']),
                        quantity=1, is_offcut=True, active=True, priority=source.priority, grain=source.grain,
                        note=f'پرت واقعی برنامه CNC #{plan.pk}', origin=source,
                        created_from_cnc_plan=plan, created_by=request.user,
                    )
                    SheetStockEvent.objects.create(
                        stock=offcut, material=plan.material, cnc_plan=plan, event_type='offcut',
                        quantity_delta=1, width=offcut.width, height=offcut.height,
                        is_offcut=True, note=f'پرت واقعی برنامه CNC #{plan.pk}', created_by=request.user,
                    )
                for task_id, count in piece_counts.items():
                    task = tasks[task_id]
                    task.completed_quantity += count
                    task.status = 'done' if task.completed_quantity >= task.quantity else 'pending'
                    task.scanned_by = request.user
                    task.save(update_fields=('completed_quantity', 'status', 'scanned_by'))

            plan.status = 'done'
            plan.result_outcome = outcome
            plan.actual_offcuts = remnants
            plan.result_note = note
            plan.completed_by = request.user
            plan.completed_at = timezone.now()
            plan.save(update_fields=('status', 'result_outcome', 'actual_offcuts', 'result_note', 'completed_by', 'completed_at'))
        messages.success(request, f'نتیجه برنامه CNC #{plan.pk} ثبت شد؛ موجودی و تسک‌ها به‌روزرسانی شدند.')
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect('cnc_resizer:sheet_plans')


@login_required
@cnc_operator_required
@require_POST
def update_planning_issue(request, issue_id):
    from django.contrib import messages
    from django.shortcuts import get_object_or_404, redirect
    issue = get_object_or_404(CncPlanningIssue, pk=issue_id)
    if not _may_access_owner(request.user, issue.created_by_id):
        raise PermissionDenied
    status = request.POST.get('status', '')
    if status not in ('open', 'resolved', 'ignored'):
        raise Http404('وضعیت issue معتبر نیست')
    note = request.POST.get('note', '').strip()[:255]
    if status == 'ignored' and not note:
        messages.error(request, 'برای نادیده‌گرفتن مورد، علت را وارد کنید.')
        return redirect('cnc_resizer:sheet_plans')
    issue.status = status
    issue.resolution_note = note
    issue.resolved_by = request.user if status != 'open' else None
    issue.resolved_at = timezone.now() if status != 'open' else None
    issue.save(update_fields=('status', 'resolution_note', 'resolved_by', 'resolved_at'))
    messages.success(request, 'وضعیت مورد بررسی به‌روزرسانی شد.')
    return redirect('cnc_resizer:sheet_plans')


@login_required
@cnc_operator_required
@require_GET
def download_sheet_plan(request, plan_id):
    from django.shortcuts import get_object_or_404
    plan = get_object_or_404(CncSheetPlan, pk=plan_id)
    if not _may_access_sheet_plan(request.user, plan):
        raise PermissionDenied
    if plan.status == 'cancelled':
        raise Http404("برنامهٔ CNC قابل دانلود نیست")
    from django.http import HttpResponse
    response = HttpResponse(plan.gcode, content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="craftflow_cnc_sheet_{plan.pk}.nc"'
    return response


def _may_access_sheet_plan(user, plan):
    return _may_access_owner(user, plan.created_by_id)


def _may_access_owner(user, owner_id):
    from cutting.permissions import can_manage
    return bool(
        user.is_superuser or user.is_staff or owner_id == user.pk or can_manage(user)
    )
