from django.http import JsonResponse
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.http import Http404

from product.decorators import admin_or_manager_required
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
        "archive_api_url": reverse("cnc_resizer:archive_files"),
        "profile_api_url": reverse("cnc_resizer:part_profile"),
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
        result = core.analyze(payload['content'], payload.get('params') or {})
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
        except CncPartProfile.DoesNotExist:
            params, approved, source_id = {'mode': 'equal', 'symmetric': True, 'axis': 'length'}, False, None
        try:
            base = part.base_part or part
            source = _base_record_for_part(base, request.user)
            with open(source.path, 'r', encoding='utf-8-sig') as stream:
                content = stream.read(core.MAX_BYTES + 1)
        except Exception as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=404)
        base = part.base_part or part
        return JsonResponse({'success': True, 'part_id': part.id, 'part_name': part.name,
                             'source_id': source_id or source.id, 'source_name': source.barcode_key,
                             'content': content, 'params': params, 'approved': approved,
                             'part_length': float(part.length), 'base_length': float(base.length)})
    import json
    try:
        payload = json.loads(request.body.decode('utf-8'))
        source_id = int(payload['source_id'])
        params = payload.get('params') or {}
        if params.get('axis', 'length') != 'length':
            raise ValueError('محور CNC باید length باشد؛ Y فایل CNC برابر Part.length است.')
        source = CncFile.objects.get(pk=source_id, kind=CncFile.KIND_BASE, resizable=True)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, CncFile.DoesNotExist) as exc:
        return JsonResponse({'success': False, 'error': str(exc) or 'اطلاعات الگو معتبر نیست.'}, status=400)
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
        x, y = float(piece['x']) + 5, float(piece['y']) + 5
        w, h = float(piece['width']), float(piece['height'])
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
    issues = CncPlanningIssue.objects.filter(created_by=request.user).select_related('task', 'order', 'part')[:100]
    active_count = sum(plan.status == 'planned' for plan in plans)
    return render(request, 'cnc_resizer/sheet_plans.html', {'plans': plans, 'issues': issues, 'active_count': active_count})


@login_required
@cnc_operator_required
@require_POST
def cancel_sheet_plan(request, plan_id):
    """Release a planned sheet so its stock and its parts can be planned again."""
    from django.contrib import messages
    from django.shortcuts import get_object_or_404, redirect
    plan = get_object_or_404(CncSheetPlan, pk=plan_id, status='planned')
    if not _may_access_sheet_plan(request.user, plan):
        raise PermissionDenied
    plan.status = 'cancelled'
    plan.save(update_fields=['status'])
    messages.success(request, f'برنامه ورق #{plan.pk} لغو شد و ورق و قطعات آن آزاد شدند.')
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
    from cutting.permissions import can_manage
    return bool(
        user.is_superuser or user.is_staff or plan.created_by_id == user.pk
        or can_manage(user)
    )
