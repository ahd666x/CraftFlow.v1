"""Controller post-processor for the legacy length-resize flow.

The workshop CNC controller dialect was reverse-engineered from 65+ real
production ``.cnc`` files supplied by the machine owner (and re-verified
against every file of the ``g-code`` archive the owner uploaded later):

* program start : ``%`` / ``G90 G54`` / ``G53 Z0`` / ``T#M6`` / ``M141`` /
  ``M6T#`` / ``G43 H#``  — ``M141`` (PLC one-shot: vacuum / purge / ATC
  handshake) appears exactly once, only in the opening tool block.
* motion        : G0/G1/G2/G3, absolute X/Y (G90), incremental I/J arc
  centres. R-format arcs never occur and are rejected.
* tool switch   : mid-program ``M6T#`` + ``G43 H#`` (the first-stage
  ``T#M6`` select and ``M141`` never repeat after the header).
* per part end  : ``G0Z<clearance>`` rapid retract, then ``G0X0Y0``
  program-zero park.
* program end   : ``G53`` machine park lines, ``M30``, ``%``. Files use
  CRLF line endings and ``N`` numbers stepping by 10.

This module is the piece missing behind the historic API error
«پست‌پروسسور مختص کنترلر تأییدشده ثبت نشده است؛ API فقط پیش‌نمایش هندسی
ارائه می‌دهد»: the length-resize engine (``core.process``) only ever
rewrites Y coordinates of an unchanged program structure. When the source
verifies against the exact controller dialect, that Y-translated program is
itself the controller-specific post-processed output — same header, same
``N`` numbering, same tool blocks, same ``G53`` park, machine-ready. Any
source outside the dialect stays fail-closed with a precise reason list.

Enable with::

    # selvi/.env
    CNC_RESIZER_IMPLICIT_UNITS=mm            # files carry no G21/G20
    CNC_RESIZER_SHEET_ADAPTER=minimal_xy_router

``CNC_RESIZER_CONTROLLER_ADAPTER`` is accepted as an alias so the machine
output of the resizer flow can be controlled independently of the sheet
planner if ever needed.
"""
from __future__ import annotations

import re

from django.conf import settings

from . import gcode_sheet, sheet_adapter

ADAPTER_KEY = sheet_adapter.DIALECT_ADAPTER_KEY  # "minimal_xy_router"

_G90_LINE_RE = re.compile(r"(G90)(?!G21)")


class ControllerPostprocessError(ValueError):
    """Source or resized program is outside the verified controller dialect.

    ``violations`` carries the per-line reason list for the UI status card.
    """

    def __init__(self, message, violations=None):
        super().__init__(message)
        self.violations = list(violations or [])


def controller_adapter_key() -> str:
    """Active controller adapter key, or '' when machine output is blocked."""
    for name in ("CNC_RESIZER_CONTROLLER_ADAPTER", "CNC_RESIZER_SHEET_ADAPTER"):
        value = str(getattr(settings, name, "") or "").strip()
        if value:
            return value
    return ""


def implicit_units_mm() -> bool:
    """True when the operator declared that unit-less sources are millimetre."""
    return str(getattr(settings, "CNC_RESIZER_IMPLICIT_UNITS", "") or "").strip().lower() == "mm"


def normalize_units(text: str) -> tuple[str, bool]:
    """Inject ``G21`` into the ``G90`` block for unit-less millimetre sources.

    Mirrors the sheet planner's normalization contract: an implicit-units
    source is accepted only when ``CNC_RESIZER_IMPLICIT_UNITS=mm`` is set,
    and the normalized text (with an explicit ``G21``) is what gets stored
    and downloaded. Returns ``(text, injected)``.
    """
    if "G21" in text:
        return text, False
    if not implicit_units_mm():
        return text, False
    lines, eol = text.splitlines(), "\r\n" if "\r\n" in text else "\n"
    out, injected = [], False
    for line in lines:
        if not injected and _G90_LINE_RE.search(line):
            out.append(_G90_LINE_RE.sub(r"\1G21", line, count=1))
            injected = True
        else:
            out.append(line)
    if not injected:  # no G90 line to anchor on — insert before first motion line
        for index, line in enumerate(out):
            if re.search(r"G[0-3](?!\d)", line):
                out.insert(index, "G21")
                injected = True
                break
    if not injected:
        out.append("G21")
        injected = True
    return eol.join(out) + eol, injected


def verify_program(text: str) -> dict:
    """Verify one program against the workshop controller dialect.

    Returns ``{"ok": bool, "violations": [...], "warnings": [...],
    "structure": {...}, "units_injected": bool}``. The structure payload
    exposes the parsed dialect facts (tool, H, M141, body size, mid-body
    switches) for the UI status card.
    """
    violations: list[str] = []
    warnings: list[str] = []
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "violations": ["فایل خالی است."], "warnings": [],
                "structure": None, "units_injected": False}

    normalized, injected = normalize_units(text)
    if injected:
        warnings.append("فایل واحد صریح نداشت؛ با تنظیم CNC_RESIZER_IMPLICIT_UNITS=mm، G21 پیش از نخستین حرکت تزریق شد.")

    structure = None
    try:
        structure = sheet_adapter.analyze_piece(normalized)
    except gcode_sheet.GcodeError as exc:
        violations.append(f"ساختار فایل قابل استفاده نیست: {exc}")
    except sheet_adapter.SheetAdapterError as exc:
        violations.append(str(exc))

    if structure is not None and not structure.get("m141"):
        warnings.append("M141 (شروع PLC کنترلر) در سربرگ یافت نشد؛ چیدمان ورق این قطعه را نمی‌پذیرد.")

    return {
        "ok": not violations,
        "violations": violations,
        "warnings": warnings,
        "structure": None if structure is None else {
            "adapter_key": ADAPTER_KEY,
            "tool": structure["tool"],
            "h": structure["h"],
            "m141": structure["m141"],
            "body_lines": len(structure["body"]),
            "mid_tool_switches": len(structure["switches"]),
            "park_lines": len(structure["post"]),
        },
        "units_injected": injected,
    }


def postprocess_resize(source_text: str, resized_text: str) -> dict:
    """Post-process a resized program into controller-ready machine text.

    Both the source and the Y-translated candidate must verify against the
    exact controller dialect and keep a 1:1 line structure (the resize may
    only rewrite Y numbers, never add/remove lines). The returned text is
    unit-normalized (explicit ``G21``) when the implicit-mm setting is on.
    """
    source_report = verify_program(source_text)
    if not source_report["ok"]:
        raise ControllerPostprocessError("؛ ".join(source_report["violations"]), source_report["violations"])

    normalized, injected = normalize_units(resized_text)
    try:
        candidate = sheet_adapter.analyze_piece(normalized)
    except (gcode_sheet.GcodeError, sheet_adapter.SheetAdapterError) as exc:
        raise ControllerPostprocessError(f"خروجی تغییر اندازه از دیالکت کنترلر خارج شد: {exc}", [str(exc)]) from exc

    source_lines = gcode_sheet.split_program(source_text)
    candidate_lines = gcode_sheet.split_program(normalized)
    if len(source_lines[0]) != len(candidate_lines[0]):
        raise ControllerPostprocessError("تعداد خطوط سربرگ فایل خروجی با فایل پایه تفاوت دارد.")
    if len(source_lines[1]) != len(candidate_lines[1]):
        raise ControllerPostprocessError("تعداد خطوط بدنهٔ فایل خروجی با فایل پایه تفاوت دارد؛ تغییر ساختار مجاز نیست.")
    if len(source_lines[2]) != len(candidate_lines[2]):
        raise ControllerPostprocessError("خطوط پارک ماشینی انتهای فایل خروجی تغییر کرده است.")

    if candidate is not None and not candidate.get("m141"):
        raise ControllerPostprocessError("M141 سربرگ در خروجی یافت نشد؛ خروجی مسدود شد.")

    return {
        "text": normalized,
        "adapter_key": ADAPTER_KEY,
        "units_injected": injected or source_report["units_injected"],
        "tool": candidate["tool"],
        "h": candidate["h"],
        "mid_tool_switches": len(candidate["switches"]),
        "source_warnings": source_report["warnings"],
    }
