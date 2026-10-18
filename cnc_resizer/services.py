import hashlib
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


def resolve_cnc_file(part, created_by=None):
    """Resolve or generate the cached CNC file for a production part.

    CNC Y is explicitly mapped to ``Part.length``. Derived files are written
    under a content-addressed filename and indexed, so a changed base file or
    profile cannot silently reuse an old result.
    """
    base = part.base_part or part
    base_record = _base_record_for_part(base, created_by)
    delta = Decimal(part.length) - Decimal(base.length)

    # A base file can be valid for direct machining even if it is not
    # compatible with the automatic resize geometry detector. That detector
    # is needed only when the production part actually changes length.
    if abs(delta) <= Decimal('0.0005'):
        return base_record

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
    with open(base_record.path, 'rb') as stream:
        source_text = stream.read(core.MAX_BYTES + 1).decode('utf-8-sig')
    from .engine.parser import ParseError, parse_program
    from .engine.postprocessor import execution_gate
    try:
        parsed_source = parse_program(source_text, filename=base_record.barcode_key)
    except ParseError as exc:
        raise CncResolutionError(f"فایل CNC در موتور جدید قابل تجزیه نیست: {exc}") from exc
    controller = base_record.controller_profile
    controller_config = controller.postprocessor_config if controller else None
    gate = execution_gate(parsed_source, controller_config, source_sha256=base_record.sha256)
    if not gate["allowed"]:
        raise CncResolutionError("تولید فایل مشتق CNC مسدود است: " + "; ".join(gate["reasons"]))
    # A registry adapter is required to serialize transformed IR. The legacy
    # text-rewriter remains preview-only and is not allowed to write a file.
    raise CncResolutionError("تولید فایل مشتق غیرفعال است؛ آداپتر پست‌پروسسور تأییدشده هنوز ثبت نشده است.")
