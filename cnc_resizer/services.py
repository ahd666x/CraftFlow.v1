import hashlib
import json
import os
import re

from decimal import Decimal
from django.conf import settings

from . import core
from .models import CncFile, CncPartProfile


BARCODE_EXTENSION_RE = re.compile(r"\.cnc$", re.IGNORECASE)
ITEM_SUFFIX_RE = re.compile(r"\.item\d+$", re.IGNORECASE)


def canonical_barcode_key(value):
    """Return the archive key without .cnc and order-item suffixes."""
    value = os.path.basename(str(value or "")).strip()
    value = BARCODE_EXTENSION_RE.sub("", value)
    return ITEM_SUFFIX_RE.sub("", value)


def cnc_filename(barcode_key, extension=".cnc"):
    return f"{canonical_barcode_key(barcode_key)}{extension}"


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def register_base_file(path, barcode_key=None, created_by=None):
    """Index a disk file and validate whether the CNC core can resize it.

    The file remains on disk. Re-uploading a changed file creates a new indexed
    revision instead of silently overwriting the previous database record.
    """
    path = os.path.abspath(os.fspath(path))
    with open(path, "rb") as stream:
        data = stream.read(core.MAX_BYTES + 1)
    digest = _sha256(data)
    key = canonical_barcode_key(barcode_key or os.path.basename(path))
    defaults = {
        "path": path,
        "created_by": created_by,
        "resizable": False,
        "validation_error": "",
        "params": {"mode": "equal", "symmetric": True},
    }
    try:
        if len(data) > core.MAX_BYTES:
            raise core.CncError("فایل CNC بیش از ۲ مگابایت است")
        result = core.process(data.decode("utf-8-sig"), {"mode": "equal", "delta": 0})
        defaults.update({
            "resizable": True,
            "length_before": result.get("length_before"),
            "length_after": result.get("length_after"),
        })
    except (UnicodeDecodeError, core.CncError, ValueError, TypeError) as exc:
        defaults["validation_error"] = str(exc)

    record, _ = CncFile.objects.update_or_create(
        barcode_key=key,
        sha256=digest,
        kind=CncFile.KIND_BASE,
        defaults=defaults,
    )
    return record


class CncResolutionError(core.CncError):
    """A production part cannot be mapped to a usable CNC file."""


def _profile_for_part(part):
    """Read the operator-approved profile for this exact production part."""
    try:
        profile = part.cnc_profile
    except CncPartProfile.DoesNotExist:
        base = part.base_part
        if not base:
            raise CncResolutionError(f"برای قطعهٔ «{part.f3}» هنوز الگوی CNC ثبت نشده است")
        try:
            profile = base.cnc_profile
        except CncPartProfile.DoesNotExist:
            raise CncResolutionError(f"برای قطعهٔ «{part.f3}» هنوز الگوی CNC ثبت نشده است")
    if not profile.approved:
        raise CncResolutionError(f"الگوی CNC قطعهٔ «{part.f3}» هنوز تأیید نشده است")
    if not profile.source_file.resizable:
        raise CncResolutionError(f"فایل پایهٔ الگوی CNC معتبر نیست: {profile.source_file.validation_error}")
    return dict(profile.params or {})


def _base_record_for_part(base, created_by=None):
    source_dir = str(getattr(settings, 'CNC_SOURCE_DIR', ''))
    extension = getattr(settings, 'CNC_FILE_EXTENSION', '.cnc')
    key = canonical_barcode_key(base.f3)
    record = CncFile.objects.filter(
        barcode_key=key,
        kind=CncFile.KIND_BASE,
    ).order_by('-created_at', '-id').first()
    if record:
        return record
    path = os.path.join(source_dir, cnc_filename(key, extension))
    if not os.path.isfile(path):
        raise CncResolutionError(f"فایل پایه CNC برای «{key}» در آرشیو پیدا نشد")
    return register_base_file(path, key, created_by)


def manual_derived_key(base_record, part_id, target_length):
    """Content-addressed key for an operator-registered manual fix.

    Deterministic in (base file revision, part, effective length), so both the
    registration endpoint and ``resolve_cnc_file`` compute the same key and a
    stale manual file can never be silently reused for a different length.
    """
    payload = f"manual|{base_record.sha256}|{int(part_id)}|{format(Decimal(str(target_length)), 'f')}"
    item_number = int(hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12], 16)
    return f"{base_record.barcode_key}.item{item_number}"


def _manual_record_for(part, base_record, target_length):
    record = CncFile.objects.filter(
        barcode_key=manual_derived_key(base_record, part.pk, target_length),
        kind=CncFile.KIND_DERIVED,
    ).first()
    if record and os.path.isfile(record.path) and (record.params or {}).get('mode') == 'manual':
        return record
    return None


def resolve_cnc_file(part, created_by=None, effective_length=None):
    """Resolve or generate the cached CNC file for a production part.

    CNC Y is explicitly mapped to ``Part.length``. ``effective_length`` lets
    the planning surface pass the operator's overridden length; the default
    keeps the legacy ``part.length`` behaviour. Derived files are written
    under a content-addressed filename and indexed, so a changed base file or
    profile cannot silently reuse an old result. An operator-registered
    manual fix (``register_manual_cnc_file``) wins for its exact length.
    """
    base = part.base_part or part
    base_record = _base_record_for_part(base, created_by)
    length = Decimal(str(effective_length)) if effective_length is not None else Decimal(part.length)
    delta = length - Decimal(base.length)

    # A base file can be valid for direct machining even if it is not
    # compatible with the automatic resize geometry detector. That detector
    # is needed only when the production part actually changes length.
    if abs(delta) <= Decimal('0.0005'):
        return base_record

    # Manual fixes registered through the workspace manual queue come first:
    # they are the operator's verified answer for exactly this length.
    manual = _manual_record_for(part, base_record, length)
    if manual is not None:
        return manual

    if not base_record.resizable:
        raise CncResolutionError(
            f"فایل پایه CNC «{base_record.barcode_key}» قابل تغییر اندازه نیست: "
            f"{base_record.validation_error}"
        )

    # A part with the same length as its base part does not need a resize
    # rule. Require an approved profile only when an actual resize is needed.
    profile = _profile_for_part(part)

    if profile.get('axis', 'length') != 'length':
        raise CncResolutionError('محور CNC انتخاب‌شده پشتیبانی نمی‌شود؛ Y باید به Part.length نگاشت شود')

    # فایل مشتقِ قطعه با همان بازنویس legacy ساخته می‌شود که هنگام ثبت فایل
    # پایه برای تشخیص `resizable` به آن اعتماد شده، و فقط با پروفایل resize
    # تأییدشدهٔ اپراتور. این یک فایل قطعهٔ قابل بازبینی است؛ خروجی ماشینِ
    # ورق همچنان پشت gate پست‌پروسسور (CncSheetPlan.output_verified /
    # generate_gcode) مسدود است و این مسیر آن را دور نمی‌زند.
    if not getattr(settings, 'CNC_DERIVED_FILES_ENABLED', True):
        raise CncResolutionError('تولید فایل مشتق غیرفعال است (CNC_DERIVED_FILES_ENABLED=False).')

    with open(base_record.path, 'rb') as stream:
        source_text = stream.read(core.MAX_BYTES + 1).decode('utf-8-sig')

    rules = {key: value for key, value in profile.items() if key != 'axis'}
    rules['delta'] = float(delta)
    try:
        result = core.process(source_text, rules)
    except core.CncError as exc:
        raise CncResolutionError(f"تولید فایل مشتق با موتور تغییر اندازه شکست خورد: {exc}") from exc
    if not result.get('resizable') or not (result.get('cnc_text') or '').strip():
        problems = result.get('profile_errors') or list(result.get('warnings') or []) or ['نتیجهٔ موتور نامعتبر است']
        raise CncResolutionError('تولید فایل مشتق مسدود شد: ' + '؛ '.join(str(item) for item in problems))

    src_lines, _ = core.split_lines(source_text)
    out_lines, _ = core.split_lines(result['cnc_text'])
    if len(src_lines) != len(out_lines):
        raise CncResolutionError('ساختار فایل مشتق با فایل پایه تفاوت دارد؛ تولید مسدود شد.')

    # خودآزمایی متریک: تغییر طول Y بردارها باید دقیقاً برابر دلتای قطعه باشد؛
    # هر انحراف یعنی خروجی موتور برای این قطعه معتبر نیست (fail-closed) و
    # ناسازگاری پروفایل تأییدشده (مثلاً مود manual با مجموع ناسازگار) همین‌جا
    # سد می‌شود، نه در مرحلهٔ nesting.
    try:
        metric_delta = float(result['length_after']) - float(result['length_before'])
    except (KeyError, TypeError, ValueError):
        raise CncResolutionError('خروجی موتور متریک طول ندارد؛ تولید فایل مشتق مسدود شد.')
    if abs(metric_delta - float(delta)) > 0.05:
        raise CncResolutionError(
            f"تغییر طول فایل مشتق ({metric_delta:+.3f}mm) با دلتای قطعه ({float(delta):+.3f}mm) "
            f"نمی‌خواند؛ تولید مسدود شد."
        )

    # هویت content-addressed: نسخهٔ فایل پایه + قواعد تأییدشده + دلتا.
    # هر تغییر در هر کدام، کلید تازه می‌سازد و نتیجهٔ قدیمی بی‌صدا استفاده
    # نمی‌شود (طبق قرارداد docstring همین تابع).
    rules_json = json.dumps(rules, sort_keys=True, default=str, separators=(',', ':'))
    item_number = int(hashlib.sha256(
        f"{base_record.sha256}|{rules_json}".encode('utf-8')
    ).hexdigest()[:12], 16)
    derived_key = f"{base_record.barcode_key}.item{item_number}"
    existing = CncFile.objects.filter(barcode_key=derived_key, kind=CncFile.KIND_DERIVED).first()
    if existing and os.path.isfile(existing.path):
        return existing

    derived_dir = str(getattr(
        settings, 'CNC_DERIVED_DIR',
        os.path.join(str(settings.MEDIA_ROOT), 'cnc_derived'),
    ))
    os.makedirs(derived_dir, exist_ok=True)
    derived_path = os.path.join(derived_dir, f"{derived_key}.cnc")
    with open(derived_path, 'w', encoding='utf-8') as stream:
        stream.write(result['cnc_text'])

    record, _ = CncFile.objects.update_or_create(
        barcode_key=derived_key,
        kind=CncFile.KIND_DERIVED,
        sha256=_sha256(result['cnc_text'].encode('utf-8')),
        defaults={
            'path': derived_path,
            'source_file': base_record,
            'params': {
                'base_sha256': base_record.sha256,
                'delta': str(delta),
                'mode': rules.get('mode', 'equal'),
                'symmetric': bool(rules.get('symmetric', True)),
                # Snapshot the exact approved resize pattern used for this
                # immutable derived file. The live profile may change later.
                'enabled': [bool(g.get('enabled', True)) for g in result.get('gaps', [])],
                'manual': list(rules.get('manual') or []),
                'tool_dia': rules.get('tool_dia', 8.0),
                'axis': profile.get('axis', 'length'),
                'resize_gaps': result.get('gaps', []),
                'families': result.get('families', []),
                'cuts': result.get('cuts', []),
                'length_before': result.get('length_before'),
                'length_after': result.get('length_after'),
                'changed_lines': len(result.get('changed_lines', [])),
                'warnings': result.get('warnings', []),
            },
            'length_before': Decimal(str(result.get('length_before'))),
            'length_after': Decimal(str(result.get('length_after'))),
            'resizable': False,
            'validation_error': '',
            'created_by': created_by,
        },
    )
    return record


MANUAL_LENGTH_TOLERANCE_MM = 0.05


def register_manual_cnc_file(task, content, created_by=None):
    """Validate + register an operator-produced CNC file for one cut task.

    Fail-closed contract (the manual queue must never store a file that does
    not match the effective spec):
    1. the text must pass the workshop-dialect verifier (controller.verify_program);
    2. the measured Y geometry must equal the task's effective length ±0.05mm;
    3. the file is indexed as a derived CncFile with params.mode='manual' for
       exactly (part, effective length) — resolve_cnc_file picks it up from
       then on;
    4. open manual_required issues of the task are resolved with a note.
    """
    import tempfile

    from django.utils import timezone as dj_timezone

    from . import controller
    from cutting.specs import SpecLockedError, effective_spec

    if not isinstance(content, str) or not content.strip():
        raise CncResolutionError('محتوای فایل دستی خالی است؛ ثبت شد نشد.')
    part = task.part
    if part is None:
        raise CncResolutionError('این تسک قطعه ندارد؛ فایل دستی معنا ندارد.')
    try:
        spec = effective_spec(task, None)
    except SpecLockedError as exc:
        raise CncResolutionError(str(exc))
    if spec.excluded:
        raise CncResolutionError('قطعهٔ خارج از برنامه فایل دستی نمی‌پذیرد؛ ابتدا آن را به برنامه برگردانید.')
    target_length = spec.length

    verification = controller.verify_program(content)
    if not verification.get('ok'):
        raise CncResolutionError(
            'فایل دستی با دیالکت Workshop تأیید نشد: '
            + '؛ '.join(str(item) for item in verification.get('violations') or ['ساختار نامعتبر'])
        )

    from .sheet_nesting import _source_geometry
    with tempfile.NamedTemporaryFile('w', suffix='.cnc', delete=False, encoding='utf-8') as stream:
        stream.write(content)
        temp_path = stream.name
    try:
        _pre, _units, min_x, min_y, max_x, max_y = _source_geometry(temp_path)
    except Exception as exc:
        raise CncResolutionError(f'هندسهٔ فایل دستی قابل اندازه‌گیری نیست: {exc}')
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    measured_length = max_y - min_y
    if abs(measured_length - float(target_length)) > MANUAL_LENGTH_TOLERANCE_MM:
        raise CncResolutionError(
            f'طول اندازه‌گیری‌شدهٔ فایل دستی ({measured_length:.3f}mm) با طول مؤثر قطعه '
            f'({float(target_length):.3f}mm) نمی‌خواند؛ ثبت شد نشد.'
        )

    base = part.base_part or part
    base_record = _base_record_for_part(base, created_by)
    key = manual_derived_key(base_record, part.pk, target_length)
    derived_dir = str(getattr(
        settings, 'CNC_DERIVED_DIR',
        os.path.join(str(settings.MEDIA_ROOT), 'cnc_derived'),
    ))
    os.makedirs(derived_dir, exist_ok=True)
    derived_path = os.path.join(derived_dir, f'{key}.cnc')
    with open(derived_path, 'w', encoding='utf-8') as stream:
        stream.write(content)

    record, _ = CncFile.objects.update_or_create(
        barcode_key=key,
        kind=CncFile.KIND_DERIVED,
        sha256=_sha256(content.encode('utf-8')),
        defaults={
            'path': derived_path,
            'source_file': base_record,
            'params': {
                'mode': 'manual',
                'manual_for_task': task.pk,
                'manual_for_part': part.pk,
                'target_length': format(target_length, 'f'),
                'base_sha256': base_record.sha256,
                'measured_length': str(Decimal(str(measured_length)).quantize(Decimal('0.001'))),
                'dialect': controller.ADAPTER_KEY,
            },
            'length_before': Decimal(base.length),
            'length_after': Decimal(str(measured_length)).quantize(Decimal('0.001')),
            'resizable': False,
            'validation_error': '',
            'created_by': created_by,
        },
    )
    from .models import CncPlanningIssue
    CncPlanningIssue.objects.filter(
        task=task, status='open', details__stage='manual_required',
    ).update(
        status='resolved',
        resolution_note=f'فایل دستی برای طول {format(target_length, "f")} ثبت و تأیید شد (فایل #{record.pk}).',
        resolved_by=created_by,
        resolved_at=dj_timezone.now(),
    )
    return record


def engine_machine_output(source_text, preview):
    """Controller-ready G-code for an engineering-engine preview candidate.

    Mirrors the legacy process flow's contract: the active controller adapter
    must be ``minimal_xy_router``, the candidate must serialize as a pure
    coordinate patch of the source text (engine.serializer), and the final
    text must re-verify against the workshop dialect with an unchanged
    header/body/post structure (controller.postprocess_resize). Fail-closed:
    returns ``{"allowed": False, "blockers": [...]}`` instead of raising for
    expected business failures.
    """
    from . import controller
    from .engine.serializer import SerializerBlocked, serialize_candidate

    if controller.controller_adapter_key() != controller.ADAPTER_KEY:
        return {"allowed": False, "blockers": [
            "پست‌پروسسور کنترلر فعال نشده است؛ برای صدور فایل دستگاه "
            f"«{controller.ADAPTER_KEY}» را در تنظیمات (CNC_RESIZER_SHEET_ADAPTER) فعال کنید."
        ]}
    try:
        serialized = serialize_candidate(source_text, preview)
        if serialized.get("inserted_line_numbers"):
            # structural candidate (array copies): the gate must verify the
            # declared insertions instead of a 1:1 line structure
            output = controller.postprocess_structural(source_text, serialized["text"], serialized)
        else:
            output = controller.postprocess_resize(source_text, serialized["text"], serialized)
    except SerializerBlocked as exc:
        return {"allowed": False, "blockers": getattr(exc, "blockers", None) or [str(exc)]}
    except controller.ControllerPostprocessError as exc:
        return {"allowed": False, "blockers": getattr(exc, "violations", None) or [str(exc)]}
    return {
        "allowed": True,
        "text": output["text"],
        "postprocessor": {
            "adapter_key": output["adapter_key"],
            "dialect": "workshop minimal XY router (G90/G54, T#M6+M141+M6T#+G43H#, I/J arcs, G53 park, N×10)",
            "units_injected": output["units_injected"],
            "tool": output["tool"],
            "h": output["h"],
            "gate": output.get("gate", "resize_1to1"),
        },
        "changed_move_count": serialized["changed_move_count"],
        "changed_line_numbers": serialized["changed_line_numbers"],
        "inserted_line_count": serialized.get("inserted_line_count", 0),
        "candidate_sha256": serialized["candidate_sha256"],
    }
