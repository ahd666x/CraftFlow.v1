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
from .models import CncEngineeringProfile, CncFile, CncPartProfile, CncPlanningIssue, CncSheetPlan, EditTemplate, ToolSet, ToolSlot, ensure_tool_slots
from product.models import Part
from .services import _base_record_for_part


def _current_tool_map():
    """{parking int: diameter float} from the operator's tool table — the DB is
    the single source of truth; the client never sends it."""
    return {s.slot: s.dia_value for s in ToolSlot.objects.all() if s.dia is not None}


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
    # راهنمای خودکار اپراتور CNC (فاز ۲): پارکینگ ابزار + آفست + نقاط فیکس
    # در قالب با json_script امن می‌شود؛ ارزیابی ممنوع/امن سمت کلاینت با
    # هندسه‌ی زنده‌ی فایل انجام می‌گیرد.
    from instructions.cnc_guide import get_active_setup, serialize_setup

    setup = serialize_setup(get_active_setup())
    return render(request, "cnc_resizer/index.html", {
        "api_url": reverse("cnc_resizer:process"),
        "analyze_api_url": reverse("cnc_resizer:analyze"),
        "engine_url": reverse("cnc_resizer:engine_workbench"),
        "archive_api_url": reverse("cnc_resizer:archive_files"),
        "profile_api_url": reverse("cnc_resizer:part_profile"),
        "sheet_plans_url": reverse("cnc_resizer:sheet_plans"),
        "tools_api_url": reverse("cnc_resizer:tools"),
        "tool_sets_api_url": reverse("cnc_resizer:tool_sets"),
        "part_id": request.GET.get("part_id", ""),
        "cnc_setup": setup,
    })


@login_required
@cnc_operator_required
@require_GET
def engine_workbench(request):
    return render(request, "cnc_resizer/engine_workbench.html", {
        "resizer_url": reverse("cnc_resizer:index"),
        "engine_analyze_url": reverse("cnc_resizer:engine_analyze"),
        "engine_preview_url": reverse("cnc_resizer:engine_preview"),
        "engine_profile_url": reverse("cnc_resizer:engine_profile"),
        "engine_preflight_url": reverse("cnc_resizer:engine_preflight"),
        "engine_output_url": reverse("cnc_resizer:engine_output"),
        "edit_templates_url": reverse("cnc_resizer:edit_templates"),
        "edit_template_delete_url": reverse("cnc_resizer:edit_template_delete", args=[0]),
        "part_templates_url": reverse("cnc_resizer:part_templates"),
        "part_template_apply_url": reverse("cnc_resizer:part_template_apply"),
        "part_template_delete_url": reverse("cnc_resizer:part_template_delete", args=[0]),
    })


@login_required
@cnc_operator_required
@require_POST
def process(request):
    """Length-resize API.

    Always returns the geometric preview (vectors + DXF drawing). The
    machine-ready G-code (``cnc_text``) is included only when the source and
    the resized candidate verify against the registered controller dialect
    post-processor (``minimal_xy_router``); anything else stays fail-closed
    with a precise, operator-readable reason list.
    """
    import json
    from . import controller
    # بدنهٔ درخواست فقط یک‌بار پارس می‌شود و هم به handle_payload می‌رود و هم
    # برای استخراج متن اصلی همین‌جا استفاده می‌شود (قبلاً دوبار پارس می‌شد).
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = None
    # میز ابزار دیتابیس، مرجع قطر ابزارهاست؛ کلاینت آن را نمی‌فرستد (اگر بفرستد بازنویسی می‌شود).
    if isinstance(payload, dict) and isinstance(payload.get('params'), dict):
        payload['params']['tool_map'] = _current_tool_map()
    status, data = core.handle_payload(payload if isinstance(payload, dict) else {})
    if status != 200 or "cnc_text" not in data:
        return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    resized_text = data.pop("cnc_text")
    data["preview_only"] = True
    data["machine_output_written"] = False
    source_text = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(source_text, str):
        source_text = ""

    def blocked(reason, violations=None, dialect=None):
        data["safe_for_execution"] = False
        data["output_blocked_reason"] = reason
        if violations:
            data["dialect_violations"] = violations
        if dialect is not None:
            data["dialect"] = dialect
        data.setdefault("warnings", []).append(reason)

    if not data.get("resizable") or not resized_text.strip() or not source_text:
        blocked("این فایل فقط نمایش داده می‌شود و قابل تغییر طول نیست؛ خروجی دستگاه تولید نشد.")
    elif controller.controller_adapter_key() != controller.ADAPTER_KEY:
        blocked(
            "پست‌پروسسور کنترلر فعال نشده است؛ برای صدور فایل دستگاه "
            f"«{controller.ADAPTER_KEY}» را در تنظیمات (CNC_RESIZER_SHEET_ADAPTER) فعال کنید."
        )
    else:
        try:
            output = controller.postprocess_resize(source_text, resized_text)
        except controller.ControllerPostprocessError as exc:
            blocked(
                f"خروجی دستگاه با پست‌پروسسور «{controller.ADAPTER_KEY}» مسدود شد: {exc}",
                violations=getattr(exc, "violations", None) or [str(exc)],
            )
        else:
            data["cnc_text"] = output["text"]
            data["safe_for_execution"] = True
            data["postprocessor"] = {
                "adapter_key": output["adapter_key"],
                "dialect": "workshop minimal XY router (G90/G54, T#M6+M141+M6T#+G43H#, I/J arcs, G53 park, N×10)",
                "units_injected": output["units_injected"],
                "tool": output["tool"],
                "h": output["h"],
            }
            data.setdefault("warnings", []).extend(output["source_warnings"])
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
@require_http_methods(['GET', 'POST'])
def tools(request):
    """The workshop tool magazine (parking T numbers 1..10 → label + diameter).
    GET returns the current table plus saved set names; POST overwrites the
    given rows. The table feeds hole compensation (DXF/preview) automatically."""
    import json
    ensure_tool_slots()
    if request.method == 'GET':
        slots = [{'slot': s.slot, 'label': s.label, 'dia': s.dia_value}
                 for s in ToolSlot.objects.all()]
        sets = list(ToolSet.objects.values_list('name', flat=True))
        return JsonResponse({'ok': True, 'slots': slots, 'sets': sets},
                            json_dumps_params={'ensure_ascii': False})
    try:
        payload = json.loads(request.body.decode('utf-8'))
        rows = payload.get('slots')
        if not isinstance(rows, list) or not rows or len(rows) > 10:
            raise ValueError
        seen = set()
        with transaction.atomic():
            for row in rows:
                slot = int(row.get('slot'))
                if not (1 <= slot <= 10) or slot in seen:
                    raise ValueError
                seen.add(slot)
                label = str(row.get('label') or '').strip()[:80]
                dia_raw = row.get('dia')
                dia = None if dia_raw in (None, '') else round(float(dia_raw), 2)
                if dia is not None and not (0.1 <= dia <= 100):
                    raise ValueError
                obj, _created = ToolSlot.objects.get_or_create(slot=slot)
                obj.label, obj.dia = label, dia
                obj.save()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'ok': False, 'error': 'مقادیر میز ابزار نامعتبر است.'}, status=400,
                            json_dumps_params={'ensure_ascii': False})
    return JsonResponse({'ok': True, 'message': 'میز ابزار ذخیره شد.'},
                        json_dumps_params={'ensure_ascii': False})


@login_required
@cnc_operator_required
@require_POST
def tool_sets(request):
    """Named snapshots of the magazine: the workshop swaps tools between jobs
    (قطعه A / قطعه B) while parking numbers stay fixed."""
    import json
    from django.core.exceptions import ObjectDoesNotExist
    try:
        payload = json.loads(request.body.decode('utf-8'))
        action = payload.get('action')
        name = str(payload.get('name') or '').strip()[:80]
        if action not in ('save', 'load', 'delete') or not name:
            raise ValueError
        if action == 'save':
            data = {}
            for s in ToolSlot.objects.all():
                if s.label or s.dia is not None:
                    data[str(s.slot)] = {'label': s.label, 'dia': s.dia_value}
            if not data:
                return JsonResponse({'ok': False, 'error': 'میز ابزار خالی است؛ اول ابزارها را ثبت کنید.'},
                                    status=400, json_dumps_params={'ensure_ascii': False})
            ToolSet.objects.update_or_create(name=name, defaults={'slots': data})
            return JsonResponse({'ok': True, 'message': f'ست «{name}» ذخیره شد.'},
                                json_dumps_params={'ensure_ascii': False})
        try:
            tool_set = ToolSet.objects.get(name=name)
        except ObjectDoesNotExist:
            return JsonResponse({'ok': False, 'error': f'ست «{name}» پیدا نشد.'}, status=404,
                                json_dumps_params={'ensure_ascii': False})
        if action == 'delete':
            tool_set.delete()
            return JsonResponse({'ok': True, 'message': f'ست «{name}» حذف شد.'},
                                json_dumps_params={'ensure_ascii': False})
        # load
        ensure_tool_slots()
        with transaction.atomic():
            for s in ToolSlot.objects.all():
                entry = tool_set.slots.get(str(s.slot))
                s.label = str((entry or {}).get('label') or '')[:80]
                dia = (entry or {}).get('dia')
                s.dia = None if dia in (None, '') else round(float(dia), 2)
                s.save()
        return JsonResponse({'ok': True, 'message': f'ست «{name}» بارگذاری شد.'},
                            json_dumps_params={'ensure_ascii': False})
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'ok': False, 'error': 'درخواست ست ابزار نامعتبر است.'}, status=400,
                            json_dumps_params={'ensure_ascii': False})


@login_required
@cnc_operator_required
@require_POST
def engine_analyze(request):
    """Parse a CNC file into the independent IR and return preview diagnostics."""
    import json
    import os
    from .engine.parser import ParseError
    from .engine.service import inspect_program
    try:
        payload = json.loads(request.body.decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('content'), str):
            raise ValueError('content must be a string')
        result = inspect_program(payload['content'], filename=os.path.basename(payload.get('filename') or 'uploaded.cnc'))
    except (TypeError, ValueError, json.JSONDecodeError, ParseError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400, json_dumps_params={'ensure_ascii': False})
    return JsonResponse(result, json_dumps_params={'ensure_ascii': False})


def _engine_candidate(payload: dict, filename: str):
    """Build an engine preview candidate from an interactive ``edits`` list
    (rigid shifts, X stretches and placement constraints, runtime-verified)
    or from a parametric ``profile``. Sending both is rejected — they do not
    compose yet."""
    from .engine.service import edits_program, preview_program
    has_edits = 'edits' in payload
    has_profile = 'profile' in payload
    if has_edits and has_profile:
        raise ValueError('edits and profile cannot be combined in phase one; send one of them')
    if has_edits:
        edits = payload.get('edits')
        if not isinstance(edits, list):
            raise ValueError('edits must be a list')
        return edits_program(payload['content'], {'edits': edits}, filename=filename)
    profile = payload.get('profile')
    if not isinstance(profile, dict):
        raise ValueError('profile must be an object')
    return preview_program(payload['content'], profile, filename=filename)


@login_required
@cnc_operator_required
@require_POST
def engine_preview(request):
    """Return geometry-only parametric preview; never returns G-code text."""
    import json
    import os
    from .engine.parametric import TransformError
    from .engine.parser import ParseError
    try:
        payload = json.loads(request.body.decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('content'), str):
            raise ValueError('content must be a string')
        filename = os.path.basename(payload.get('filename') or 'uploaded.cnc')
        profile = payload.get('profile')
        result = _engine_candidate(payload, filename)
        if 'edits' in payload and 'profile' not in payload:
            pass  # interactive edits carry their own runtime verification
        else:
            interpretations = profile.get('operator_interpretations') or {}
            if not isinstance(interpretations, dict):
                raise ValueError('operator_interpretations must be an object')
            known_ids = {feature['id'] for feature in result['original']['features']}
            if set(interpretations) - known_ids:
                raise ValueError('operator_interpretations contains unknown feature IDs')
            allowed_interpretations = {
                'unconfirmed', 'hole_or_loop_candidate', 'slot_or_groove_candidate',
                'surface_or_raster_candidate', 'outer_contour_candidate', 'other_candidate',
            }
            invalid = {key: value for key, value in interpretations.items() if value not in allowed_interpretations}
            if invalid:
                raise ValueError('unsupported operator interpretation label')
            result['operator_review'] = {
                'physical_operation_confirmed': False,
                'features': [{
                    'feature_id': feature['id'],
                    'selected_interpretation': interpretations.get(feature['id'], 'unconfirmed'),
                    'transform_rule': (profile['features'][feature['id']].get('mode')
                                       if isinstance(profile['features'][feature['id']], dict)
                                       else profile['features'][feature['id']]),
                } for feature in result['original']['features']],
            }
    except (TypeError, ValueError, json.JSONDecodeError, ParseError, TransformError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400, json_dumps_params={'ensure_ascii': False})
    return JsonResponse(result, json_dumps_params={'ensure_ascii': False})


@login_required
@cnc_operator_required
@require_http_methods(['GET', 'POST'])
def engine_profile(request):
    """Load/save a user's durable engineering rules, bound to the parsed source hash."""
    import hashlib
    import json
    import os
    import re
    from .engine.parametric import TransformError
    from .engine.parser import ParseError, parse_program
    from .engine.recognition import recognize
    from .engine.service import preview_program
    if request.method == 'GET':
        digest = request.GET.get('sha256', '')
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            return JsonResponse({'ok': False, 'error': 'source sha256 نامعتبر است.'}, status=400)
        record = CncEngineeringProfile.objects.filter(owner=request.user, source_sha256=digest).first()
        if not record:
            return JsonResponse({'ok': True, 'found': False, 'source_sha256': digest})
        return JsonResponse({'ok': True, 'found': True, 'source_sha256': digest,
                             'filename': record.filename, 'profile': record.profile,
                             'updated_at': record.updated_at.isoformat()})
    try:
        payload = json.loads(request.body.decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('content'), str):
            raise ValueError('content باید متن CNC باشد.')
        profile = payload.get('profile')
        if not isinstance(profile, dict):
            raise ValueError('profile باید یک شیء باشد.')
        content = payload['content']
        parsed = parse_program(content, filename=os.path.basename(payload.get('filename') or 'uploaded.cnc'))
        # Run all finite-number, feature-ID, dependency-cycle, arc and geometry constraints
        # before persisting. Failed targets may still be saved for later editing; malformed
        # rules and transforms that cannot even be evaluated are not.
        preview_program(content, profile, filename=parsed.filename)
        interpretations = profile.get('operator_interpretations') or {}
        if not isinstance(interpretations, dict):
            raise ValueError('operator_interpretations باید یک شیء باشد.')
        feature_ids = {feature['id'] for feature in recognize(parsed)['features']}
        if set(interpretations) - feature_ids:
            raise ValueError('تفسیر شامل شناسهٔ ویژگی ناشناخته است.')
        allowed_interpretations = {'unconfirmed', 'hole_or_loop_candidate', 'slot_or_groove_candidate',
                                   'surface_or_raster_candidate', 'outer_contour_candidate', 'other_candidate'}
        if any(value not in allowed_interpretations for value in interpretations.values()):
            raise ValueError('برچسب تفسیر اپراتور معتبر نیست.')
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        record, _ = CncEngineeringProfile.objects.update_or_create(
            owner=request.user, source_sha256=digest,
            defaults={'filename': parsed.filename[:255], 'profile': profile},
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, ParseError, TransformError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400, json_dumps_params={'ensure_ascii': False})
    return JsonResponse({'ok': True, 'saved': True, 'source_sha256': digest,
                         'profile_id': record.pk, 'updated_at': record.updated_at.isoformat()},
                        json_dumps_params={'ensure_ascii': False})


_EDIT_TEMPLATE_OPS = frozenset({"shift", "resize_x", "array", "grid", "mirror", "rotate",
                                "center_on", "set_gap", "center_between"})


def _validate_template_payload(edits):
    """Structural validation ONLY: a template is a saved FORM, never an
    executable artifact. Geometry, depth-repeat groups, collisions and the
    runtime proof are all enforced by the preview endpoint at USE time."""
    if not isinstance(edits, list) or not edits:
        raise ValueError('ویرایش‌های قالب باید یک فهرست غیرخالی باشد.')
    if len(edits) > 64:
        raise ValueError('حداکثر ۶۴ ویرایش در هر قالب ذخیره می‌شود.')
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f'ویرایش {index + 1} باید یک شیء باشد.')
        op = edit.get('op')
        if not isinstance(op, str) or op not in _EDIT_TEMPLATE_OPS:
            raise ValueError(f'ویرایش {index + 1}: نوع ویرایش (op) نامعتبر است.')
        if not isinstance(edit.get('feature_id'), str) or not edit['feature_id']:
            raise ValueError(f'ویرایش {index + 1}: feature_id باید رشتهٔ غیرخالی باشد.')
    return edits


@login_required
@cnc_operator_required
@require_http_methods(['GET', 'POST'])
def edit_templates(request):
    """Named reusable workbench edit sets (قالب ویرایش), shared workshop-wide.

    GET lists them; POST upserts by name. A template never executes by
    itself: loading only prefills the workbench edit list, and every edit
    still passes the full fail-closed preview (runtime proof + collision
    gate) before any machine output can exist."""
    import json
    if request.method == 'GET':
        templates = [{'id': template.pk, 'name': template.name, 'note': template.note,
                      'payload': template.payload, 'updated_at': template.updated_at.isoformat()}
                     for template in EditTemplate.objects.all()]
        return JsonResponse({'ok': True, 'templates': templates},
                            json_dumps_params={'ensure_ascii': False})
    try:
        payload = json.loads(request.body.decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('درخواست قالب ویرایش نامعتبر است.')
        name = str(payload.get('name') or '').strip()[:80]
        if not name:
            raise ValueError('نام قالب لازم است.')
        edits = _validate_template_payload(payload.get('edits'))
        note = str(payload.get('note') or '').strip()[:200]
        template, created = EditTemplate.objects.update_or_create(
            name=name, defaults={'payload': edits, 'note': note, 'created_by': request.user})
        return JsonResponse({'ok': True, 'created': created, 'template_id': template.pk,
                             'message': f'قالب «{name}» ذخیره شد.'},
                            json_dumps_params={'ensure_ascii': False})
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        message = str(exc) or 'درخواست قالب ویرایش نامعتبر است.'
        return JsonResponse({'ok': False, 'error': message}, status=400,
                            json_dumps_params={'ensure_ascii': False})


@login_required
@cnc_operator_required
@require_POST
def edit_template_delete(request, template_id: int):
    import json
    from django.core.exceptions import ObjectDoesNotExist
    try:
        template = EditTemplate.objects.get(pk=template_id)
    except ObjectDoesNotExist:
        return JsonResponse({'ok': False, 'error': f'قالب شمارهٔ {template_id} پیدا نشد.'}, status=404,
                            json_dumps_params={'ensure_ascii': False})
    name = template.name
    template.delete()
    return JsonResponse({'ok': True, 'message': f'قالب «{name}» حذف شد.'},
                        json_dumps_params={'ensure_ascii': False})


@login_required
@cnc_operator_required
@require_http_methods(['GET', 'POST'])
def part_templates(request):
    """Parametric part templates (قالب پارامتریک قطعه — بستهٔ ق).

    GET lists saved part templates plus the part options (parts that carry a
    CNC profile) for the save dialog. POST captures the current edit list as
    geometry roles against the named part: the payload keeps the original
    edit ops, the roles keep kind + normalized center + size of every
    referenced feature so a NEW file of the same part can be matched later.
    Saving never executes anything; applying re-matches roles and still runs
    the full fail-closed preview chain."""
    import hashlib
    import json
    import os
    from .engine.parametric import TransformError
    from .engine.parser import ParseError, parse_program
    from .engine.recognition import recognize
    from .models import CncPartProfile, CncPartTemplate
    from .part_templates import capture_roles, usable_features
    if request.method == 'GET':
        templates = []
        for template in CncPartTemplate.objects.select_related('part').all():
            part = template.part
            templates.append({
                'id': template.pk, 'name': template.name, 'note': template.note,
                'part_id': part.pk, 'part_label': str(part),
                'payload': template.payload, 'roles': template.roles,
                'source_feature_count': template.source_feature_count,
                'updated_at': template.updated_at.isoformat(),
            })
        part_options = [{'id': profile.part_id,
                         'label': str(profile.part)}
                        for profile in CncPartProfile.objects.select_related('part').all()[:300]]
        return JsonResponse({'ok': True, 'templates': templates, 'part_options': part_options},
                            json_dumps_params={'ensure_ascii': False})
    try:
        payload = json.loads(request.body.decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('درخواست قالب قطعه نامعتبر است.')
        name = str(payload.get('name') or '').strip()[:80]
        if not name:
            raise ValueError('نام قالب قطعه لازم است.')
        part_id = payload.get('part_id')
        if not isinstance(part_id, int):
            raise ValueError('قطعهٔ مقصد قالب را انتخاب کنید.')
        edits = _validate_template_payload(payload.get('edits'))
        note = str(payload.get('note') or '').strip()[:200]
        content = payload.get('content')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('متن CNC منبع قالب لازم است.')
        part = Part.objects.filter(pk=part_id).first()
        if part is None:
            raise ValueError(f'قطعهٔ شمارهٔ {part_id} پیدا نشد.')
        parsed = parse_program(content, filename=os.path.basename(payload.get('filename') or 'uploaded.cnc'))
        features = recognize(parsed)['features']
        roles = capture_roles(features, edits)
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        template, created = CncPartTemplate.objects.update_or_create(
            part=part, name=name,
            defaults={'payload': edits, 'roles': roles,
                      'source_bounds': capture_envelope_for_storage(features),
                      'source_feature_count': len(usable_features(features)),
                      'source_sha256': digest,
                      'source_filename': parsed.filename[:255],
                      'note': note, 'created_by': request.user})
        return JsonResponse({'ok': True, 'created': created, 'template_id': template.pk,
                             'role_count': len(roles),
                             'message': f'قالب قطعهٔ «{name}» برای {part} ذخیره شد ({len(roles)} نقش).'},
                            json_dumps_params={'ensure_ascii': False})
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, ParseError, TransformError) as exc:
        message = str(exc) or 'درخواست قالب قطعه نامعتبر است.'
        return JsonResponse({'ok': False, 'error': message}, status=400,
                            json_dumps_params={'ensure_ascii': False})


def capture_envelope_for_storage(features):
    """Envelope dict (with size) of the source analysis, for role matching."""
    from .part_templates import envelope_of_features, usable_features
    envelope = envelope_of_features(usable_features(features))
    envelope['width'] = envelope['x_max'] - envelope['x_min']
    envelope['height'] = envelope['y_max'] - envelope['y_min']
    return envelope


@login_required
@cnc_operator_required
@require_POST
def part_template_apply(request):
    """Apply a part template to the posted file content (بستهٔ ق).

    Roles are re-matched against this file's analysis; the materialized
    concrete edits go through the ordinary fail-closed ``edits_program``
    preview (runtime reparse + collision gate). The response carries the
    same preview payload the preview endpoint returns, plus the concrete
    edit list and the per-role mapping report."""
    import json
    import os
    from .engine.parametric import TransformError
    from .engine.parser import ParseError, parse_program
    from .engine.recognition import recognize
    from .engine.service import edits_program
    from .models import CncPartTemplate
    from .part_templates import match_roles, materialize_edits
    try:
        payload = json.loads(request.body.decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('content'), str):
            raise ValueError('content must be a string')
        template_id = payload.get('template_id')
        if not isinstance(template_id, int):
            raise ValueError('قالب قطعه انتخاب نشده است.')
        template = CncPartTemplate.objects.select_related('part').filter(pk=template_id).first()
        if template is None:
            raise ValueError(f'قالب قطعهٔ شمارهٔ {template_id} پیدا نشد.')
        content = payload['content']
        filename = os.path.basename(payload.get('filename') or 'uploaded.cnc')
        parsed = parse_program(content, filename=filename)
        features = recognize(parsed)['features']
        mapping, report = match_roles(template.roles, template.source_bounds, features,
                                      source_feature_count=template.source_feature_count,
                                      edits=template.payload)
        concrete = materialize_edits(template.payload, mapping)
        preview = edits_program(content, {'edits': concrete}, filename=filename)
        return JsonResponse({'ok': True, 'edits': concrete, 'mapping': report,
                             'preview': preview,
                             'template': {'id': template.pk, 'name': template.name,
                                          'part_label': str(template.part)}},
                            json_dumps_params={'ensure_ascii': False})
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, ParseError, TransformError) as exc:
        message = str(exc) or 'بارگذاری قالب قطعه ناموفق بود.'
        return JsonResponse({'ok': False, 'error': message}, status=400,
                            json_dumps_params={'ensure_ascii': False})


@login_required
@cnc_operator_required
@require_POST
def part_template_delete(request, template_id: int):
    import json
    from django.core.exceptions import ObjectDoesNotExist
    from .models import CncPartTemplate
    try:
        template = CncPartTemplate.objects.get(pk=template_id)
    except ObjectDoesNotExist:
        return JsonResponse({'ok': False, 'error': f'قالب قطعهٔ شمارهٔ {template_id} پیدا نشد.'}, status=404,
                            json_dumps_params={'ensure_ascii': False})
    name = template.name
    template.delete()
    return JsonResponse({'ok': True, 'message': f'قالب قطعهٔ «{name}» حذف شد.'},
                        json_dumps_params={'ensure_ascii': False})


@login_required
@cnc_operator_required
@require_POST
def engine_preflight(request):
    """Stage-seven output-readiness report; deliberately emits no machine file.

    When the verified controller adapter is active, the report runs the real
    dialect bridge (serializer + postprocess_resize) so ``allowed`` reflects
    the actual machine-output path instead of a permanent blocker.
    """
    import json
    import os
    from . import controller
    from .engine.output_validation import validate_output_candidate
    from .engine.parametric import TransformError
    from .engine.parser import ParseError, parse_program
    try:
        payload = json.loads(request.body.decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('content'), str):
            raise ValueError('content باید متن CNC باشد.')
        filename = os.path.basename(payload.get('filename') or 'uploaded.cnc')
        source = parse_program(payload['content'], filename=filename)
        candidate = _engine_candidate(payload, filename)
        gate = (controller.postprocess_resize
                if controller.controller_adapter_key() == controller.ADAPTER_KEY else None)
        report = validate_output_candidate(
            source, candidate, payload.get('machine_limits'),
            source_text=payload['content'] if gate else None, dialect_gate=gate)
    except (TypeError, ValueError, json.JSONDecodeError, ParseError, TransformError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400, json_dumps_params={'ensure_ascii': False})
    return JsonResponse({'ok': True, 'preflight': report}, json_dumps_params={'ensure_ascii': False})


@login_required
@cnc_operator_required
@require_POST
def engine_output(request):
    """Controller-ready G-code for an engine candidate via the verified dialect gate.

    The candidate must serialize as a pure coordinate patch of the source and
    the final text must re-verify against the workshop controller dialect
    (minimal_xy_router); anything else stays fail-closed with precise reasons.
    """
    import json
    import os
    from .engine.parametric import TransformError
    from .engine.parser import ParseError
    from .services import engine_machine_output
    try:
        payload = json.loads(request.body.decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('content'), str):
            raise ValueError('content باید متن CNC باشد.')
        filename = os.path.basename(payload.get('filename') or 'uploaded.cnc')
        candidate = _engine_candidate(payload, filename)
        result = engine_machine_output(payload['content'], candidate)
    except (TypeError, ValueError, json.JSONDecodeError, ParseError, TransformError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400, json_dumps_params={'ensure_ascii': False})
    if not result.get('allowed'):
        return JsonResponse({'ok': True, 'allowed': False, 'safe_for_execution': False,
                             'blockers': result.get('blockers', [])},
                            json_dumps_params={'ensure_ascii': False})
    return JsonResponse({'ok': True, 'allowed': True, 'safe_for_execution': True,
                         'cnc_text': result['text'], 'postprocessor': result['postprocessor'],
                         'changed_move_count': result['changed_move_count'],
                         'changed_line_numbers': result['changed_line_numbers'],
                         'inserted_line_count': result.get('inserted_line_count', 0),
                         'candidate_sha256': result['candidate_sha256']},
                        json_dumps_params={'ensure_ascii': False})


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
            'label': piece.get('f3') or piece.get('label') or '',
            'width_mm': w, 'height_mm': h, 'rotated': piece.get('rotated', False),
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
        plan.unused_area = max(0, plan.width * plan.height - plan.planned_area)
        total_area = float(plan.width) * float(plan.height)
        plan.utilization = round(float(plan.planned_area) / total_area * 100, 1) if total_area else 0
        plan.unused_area_m2 = round(plan.unused_area / 1_000_000, 2)
        plan.output_status = 'فایل دستگاه تأییدشده' if plan.output_verified and plan.gcode.strip() else 'نوع خروجی نامشخص؛ فایل تولید نشده'
    issue_query = CncPlanningIssue.objects.all() if can_view_all else CncPlanningIssue.objects.filter(created_by=request.user)
    issues = issue_query.select_related('task', 'order', 'part', 'resolved_by')[:100]
    issue_count = issue_query.count()
    total_count = plan_query.count()
    active_count = plan_query.filter(status='planned').count()
    used_count = plan_query.filter(status='done', result_outcome='used').count()
    return render(request, 'cnc_resizer/sheet_plans.html', {
        'plans': plans, 'issues': issues, 'total_count': total_count, 'issue_count': issue_count,
        'active_count': active_count, 'used_count': used_count,
    })


@login_required
@cnc_operator_required
@require_POST
def cancel_sheet_plan(request, plan_id):
    """Release a planned sheet so its stock and its parts can be planned again."""
    from django.contrib import messages
    from django.shortcuts import get_object_or_404, redirect
    with transaction.atomic():
        plan = get_object_or_404(CncSheetPlan.objects.select_for_update().select_related('source_stock', 'material'), pk=plan_id)
        if not _may_access_sheet_plan(request.user, plan):
            raise PermissionDenied
        if plan.status == 'cancelled':
            messages.info(request, f'برنامه CNC #{plan.pk} قبلاً لغو شده است؛ موجودی تغییری نکرد.')
            return redirect('cnc_resizer:sheet_plans')
        if plan.status != 'planned':
            messages.info(request, f'برنامه CNC #{plan.pk} نهایی شده و قابل لغو نیست.')
            return redirect('cnc_resizer:sheet_plans')
        plan.status = 'cancelled'
        plan.save(update_fields=['status'])
        from cutting.models import SheetStockEvent
        SheetStockEvent.objects.create(
            stock=plan.source_stock, material=plan.material, cnc_plan=plan,
            event_type='release', quantity_delta=0, width=plan.width, height=plan.height,
            is_offcut=False, note=f'لغو برنامه CNC #{plan.pk}', created_by=request.user,
        )
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
                pk=plan_id,
            )
            if not _may_access_sheet_plan(request.user, plan):
                raise PermissionDenied
            if plan.status == 'done':
                messages.info(request, f'نتیجهٔ برنامه CNC #{plan.pk} قبلاً ثبت شده است؛ موجودی دوباره تغییر نکرد.')
                return redirect('cnc_resizer:sheet_plans')
            if plan.status != 'planned':
                raise ValueError('برنامهٔ لغوشده قابل ثبت نتیجه نیست.')
            outcome = request.POST.get('outcome', '')
            note = request.POST.get('note', '').strip()[:255]
            if outcome not in ('used', 'untouched', 'scrapped'):
                raise ValueError('نتیجهٔ ورق معتبر نیست.')
            if outcome == 'scrapped' and not note:
                raise ValueError('برای اسقاط ورق، ثبت علت الزامی است.')
            # نبود فایل دستگاه تأییدشده فقط «مصرف شد» را مسدود می‌کند؛
            # «استفاده نشد» و «اسقاط» دقیقاً برای زمانی طراحی شده‌اند که
            # فایل دستگاه تولید نشده یا اجرای آن ممکن نبوده — بستنِ ثبت
            # دفتریِ آن‌ها برنامهٔ planned را برای همیشه قفل می‌کرد.
            if outcome == 'used' and (not plan.output_verified or not plan.gcode.strip()):
                raise ValueError('فایل خروجی تأییدشده‌ای برای این برنامه وجود ندارد؛ تکمیل قطعات «مصرف شد» مسدود است.')

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
                remnant_area = sum((Decimal(r['width']) * Decimal(r['height']) for r in remnants), Decimal('0'))
                if remnant_area > plan.width * plan.height:
                    raise ValueError('مجموع مساحت پرت‌ها از مساحت ورق مبدأ بیشتر است.')
                if remnants and (plan.planned_area <= 0 or remnant_area > plan.width * plan.height - plan.planned_area):
                    raise ValueError('مجموع مساحت پرت‌ها از فضای باقی‌مانده پس از قطعات برنامه بیشتر است؛ نتیجه ثبت نشد.')

            source = get_object_or_404(SheetStock.objects.select_for_update(), pk=plan.source_stock_id)
            if not source.active or source.quantity < 1:
                raise ValueError('ورق رزروشده دیگر در موجودی فعال نیست؛ نتیجه ثبت نشد.')
            if outcome in ('used', 'scrapped'):
                from cutting.views import _reserved_counts
                future_reserved = _reserved_counts([source.pk])[source.pk] - 1
                if source.quantity - 1 < future_reserved:
                    raise ValueError('کسر این ورق باعث کمبود رزرو برنامهٔ دیگری می‌شود.')

            piece_counts = Counter()
            piece_snapshots = {}
            for piece in (plan.layout or {}).get('parts', []):
                if piece.get('task_id') is not None:
                    task_id = int(piece['task_id'])
                    piece_counts[task_id] += 1
                    snapshot = {
                        'part_id': piece.get('part_id'), 'order_id': piece.get('order_id'),
                        'task_quantity': piece.get('task_quantity'),
                        'completed_before': piece.get('completed_before'),
                    }
                    if outcome == 'used' and any(value is None for value in snapshot.values()):
                        raise ValueError('برنامهٔ CNC تصویر ثابت کامل تسک‌های زمان برنامه‌ریزی را ندارد؛ نتیجه ثبت نشد.')
                    if outcome == 'used' and task_id in piece_snapshots and piece_snapshots[task_id] != snapshot:
                        raise ValueError('تصویر ثابت تسک در برنامه CNC ناسازگار است؛ نتیجه ثبت نشد.')
                    piece_snapshots[task_id] = snapshot
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
                    snapshot = piece_snapshots[task_id]
                    if (task.status != 'pending' or task.part_id != snapshot['part_id']
                            or task.order_id != snapshot['order_id'] or task.quantity != snapshot['task_quantity']
                            or task.completed_quantity != snapshot['completed_before']
                            or task.completed_quantity + count > task.quantity):
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
    if plan.status == 'cancelled' or not plan.output_verified or not plan.gcode.strip():
        from django.http import HttpResponse
        return HttpResponse(
            'فایل خروجی CNC معتبر و تأییدشده‌ای برای این برنامه وجود ندارد.',
            status=409, content_type='text/plain; charset=utf-8',
        )
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
