from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST
from django.http import Http404

from product.decorators import admin_or_manager_required
from . import core
from .models import CncFile, CncPartProfile
from product.models import Part
from .services import _base_record_for_part


def cnc_operator_required(view_func):
    """Allow managers and the CNC worker who opened a part from scanning."""
    from functools import wraps
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        user = request.user
        allowed = user.is_superuser or user.groups.filter(
            name__in=['1', '2', '3', 'نماینده']
        ).exists()
        try:
            allowed = allowed or user.workerprofile.stage == 'cnc'
        except Exception:
            pass
        if not allowed:
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
@require_GET
def archive_files(request):
    record_id = request.GET.get("id")
    if record_id:
        record = CncFile.objects.filter(
            pk=record_id, kind=CncFile.KIND_BASE, resizable=True
        ).first()
        if not record or not record.path:
            raise Http404("فایل CNC آرشیو پیدا نشد")
        try:
            with open(record.path, "r", encoding="utf-8-sig") as stream:
                content = stream.read(core.MAX_BYTES + 1)
        except OSError as exc:
            raise Http404(f"خواندن فایل CNC ممکن نیست: {exc}")
        return JsonResponse({
            "id": record.id,
            "name": record.barcode_key,
            "content": content,
        }, json_dumps_params={"ensure_ascii": False})

    records = CncFile.objects.filter(
        kind=CncFile.KIND_BASE, resizable=True
    ).values("id", "barcode_key", "length_before", "created_at")
    return JsonResponse({
        "files": [
            {
                "id": row["id"],
                "name": row["barcode_key"],
                "length": row["length_before"],
            }
            for row in records
        ]
    }, json_dumps_params={"ensure_ascii": False})


@login_required
@cnc_operator_required
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
