import hashlib
import os
import re

from . import core
from .models import CncFile


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
