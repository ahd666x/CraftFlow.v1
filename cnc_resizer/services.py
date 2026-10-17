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
    if not base_record.resizable:
        raise CncResolutionError(
            f"فایل پایه CNC «{base_record.barcode_key}» قابل تغییر اندازه نیست: "
            f"{base_record.validation_error}"
        )
    delta = Decimal(part.length) - Decimal(base.length)
    if abs(delta) <= Decimal('0.0005'):
        return base_record

    # A part with the same length as its base part does not need a resize
    # rule. Require an approved profile only when an actual resize is needed.
    profile = _profile_for_part(part)

    if profile.get('axis', 'length') != 'length':
        raise CncResolutionError('محور CNC انتخاب‌شده پشتیبانی نمی‌شود؛ Y باید به Part.length نگاشت شود')
    params = {
        'mode': profile.get('mode', 'equal'),
        'delta': float(delta),
        'symmetric': bool(profile.get('symmetric', True)),
    }
    for key in ('enabled', 'manual', 'tool_dia'):
        if key in profile:
            params[key] = profile[key]
    source_params = dict(params)
    source_params['source_sha256'] = base_record.sha256
    source_params['axis'] = 'length'

    with open(base_record.path, 'rb') as stream:
        source_text = stream.read(core.MAX_BYTES + 1).decode('utf-8-sig')
    result = core.process(source_text, params)
    actual_delta = Decimal(str(result['length_after'])) - Decimal(str(result['length_before']))
    if abs(actual_delta - delta) > Decimal('0.1'):
        raise CncResolutionError(
            f"delta فایل CNC با مقدار خواسته‌شده برابر نیست: {actual_delta} در برابر {delta}"
        )

    extension = getattr(settings, 'CNC_FILE_EXTENSION', '.cnc')
    key = canonical_barcode_key(part.f3)
    digest = _sha256(result['cnc_text'].encode('utf-8'))
    filename = f"{key}.derived-{digest[:12]}{extension}"
    path = os.path.join(str(getattr(settings, 'CNC_SOURCE_DIR', '')), filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as stream:
        stream.write(result['cnc_text'])
    record, _ = CncFile.objects.update_or_create(
        barcode_key=key,
        sha256=digest,
        kind=CncFile.KIND_DERIVED,
        defaults={
            'path': path,
            'source_file': base_record,
            'params': source_params,
            'length_before': result['length_before'],
            'length_after': result['length_after'],
            'resizable': True,
            'validation_error': '',
            'created_by': created_by,
        },
    )
    return record
