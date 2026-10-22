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
_G20_WORD_RE = re.compile(r"G20(?![\d.])")
_G21_WORD_RE = re.compile(r"G21(?![\d.])")


def _has_unit_word(text: str, word_re: re.Pattern) -> bool:
    """Comment-safe G20/G21 word search (a 'G21' inside a comment or glued to
    digits like 'G91.1' must not count as a unit declaration)."""
    for line in text.splitlines():
        cleaned = gcode_sheet._clean(line)
        if cleaned and word_re.search(cleaned):
            return True
    return False


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


def unapproved_controller_blocker() -> str | None:
    """DB governance kill-switch over the machine-output path (bugfix).

    An ACTIVE ``CncControllerProfile`` row for this adapter whose ``approved``
    is False blocks every machine-output gate — «تأیید حسابرسی‌شده» was
    previously a dead flag that nothing in the output path consulted. With no
    profile row at all, the documented settings-only behaviour is unchanged
    (the .env enablement keeps working for deployments that do not use the
    audit admin)."""
    try:
        from .models import CncControllerProfile
        profile = (CncControllerProfile.objects
                   .filter(adapter_key=ADAPTER_KEY, active=True)
                   .order_by("-updated_at").first())
    except Exception:
        # The governance layer is additive on top of the settings gate: when
        # the DB is unavailable (or a SimpleTestCase forbids queries), the
        # documented settings-only behaviour must keep working — never take
        # the machine-output path down because of the audit table.
        return None
    if profile is not None and not profile.approved:
        return (f"پروفایل کنترلر فعال «{profile.key}» تأیید حسابرسی ندارد (approved=False)؛ "
                "خروجی دستگاه مسدود است — پروفایل را در ادمین تأیید کنید یا غیرفعالش کنید.")
    return None


def implicit_units_mm() -> bool:
    """True when the operator declared that unit-less sources are millimetre."""
    return str(getattr(settings, "CNC_RESIZER_IMPLICIT_UNITS", "") or "").strip().lower() == "mm"


def normalize_units(text: str) -> tuple[str, bool]:
    """Substitute ``G21`` INTO the ``G90`` block for unit-less millimetre sources.

    Mirrors the sheet planner's normalization contract: an implicit-units
    source is accepted only when ``CNC_RESIZER_IMPLICIT_UNITS=mm`` is set,
    and the normalized text (with an explicit ``G21``) is what gets stored
    and downloaded. Returns ``(text, injected)``.

    bugfix contract: this function NEVER changes the line count. The old
    fallback inserted a standalone ``G21`` line when no ``G90`` anchor exists,
    which broke the 1:1 header check of ``postprocess_resize`` with the
    misleading «تعداد خطوط سربرگ تفاوت دارد». Now:
    * an explicit ``G20`` (inch) file is NOT unit-less — no injection, the
      real unit conflict surfaces in the dialect report instead;
    * without a ``G90`` anchor the text is returned untouched and the dialect
      verification reports the actual structural reason.
    """
    if _has_unit_word(text, _G21_WORD_RE):
        return text, False
    if not implicit_units_mm():
        return text, False
    if _has_unit_word(text, _G20_WORD_RE):
        # explicit inch units: the mm assumption does not apply (the file is
        # not unit-less); the dialect gate reports the true cause downstream.
        return text, False
    lines, eol = text.splitlines(), "\r\n" if "\r\n" in text else "\n"
    out, injected = [], False
    for line in lines:
        if not injected and _G90_LINE_RE.search(line):
            out.append(_G90_LINE_RE.sub(r"\1G21", line, count=1))
            injected = True
        else:
            out.append(line)
    if not injected:
        # no G90 anchor: inserting a line would silently break the header
        # line-count contract of the resize/structural gates — leave as-is.
        return text, False
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


def postprocess_resize(source_text: str, resized_text: str, serialized: dict | None = None) -> dict:
    """Post-process a resized program into controller-ready machine text.

    Both the source and the Y-translated candidate must verify against the
    exact controller dialect and keep a 1:1 line structure (the resize may
    only rewrite Y numbers, never add/remove lines). The returned text is
    unit-normalized (explicit ``G21``) when the implicit-mm setting is on.
    ``serialized`` is accepted for signature parity with
    :func:`postprocess_structural` and ignored.
    """
    source_report = verify_program(source_text)
    if not source_report["ok"]:
        raise ControllerPostprocessError("؛ ".join(source_report["violations"]), source_report["violations"])

    blocker = unapproved_controller_blocker()
    if blocker:
        raise ControllerPostprocessError(blocker, [blocker])

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


def _line_signature(line: str):
    """Patch-insensitive word signature of one raw G-code line.

    Comments, ``N`` numbers and blank lines are ignored (N numbering is
    stripped by design on array copy lines; the dialect gate ignores it).
    Coordinate letters keep only their letter (values may be patched);
    ``G2``/``G3`` count as one signature token because mirroring legally
    swaps them; every other word keeps its raw value.
    """
    cleaned = gcode_sheet._clean(line)
    if not cleaned:
        return None
    signature = []
    for letter, value, raw in gcode_sheet._words(cleaned):
        if letter in "XYIJ":
            signature.append((letter, letter))
        elif letter == "G" and value in (2.0, 3.0):
            signature.append(("G", "2|3"))
        else:
            signature.append((letter, raw))
    return tuple(signature)


def postprocess_structural(source_text: str, resized_text: str, serialized: dict | None = None) -> dict:
    """Dialect gate for structural candidates (interactive array copies).

    Identical to :func:`postprocess_resize` except that declared line
    insertions are allowed: the serializer reports the final 1-based
    ``inserted_line_numbers`` of its copy lines, and this gate proves the
    remaining candidate lines correspond 1:1 IN ORDER to the source lines
    with patch-insensitive signatures. Everything else — full dialect
    verification of the final text, preamble/postamble preservation, M141 —
    stays as strict as the resize gate.
    """
    source_report = verify_program(source_text)
    if not source_report["ok"]:
        raise ControllerPostprocessError("؛ ".join(source_report["violations"]), source_report["violations"])

    blocker = unapproved_controller_blocker()
    if blocker:
        raise ControllerPostprocessError(blocker, [blocker])

    normalized, injected = normalize_units(resized_text)
    try:
        candidate = sheet_adapter.analyze_piece(normalized)
    except (gcode_sheet.GcodeError, sheet_adapter.SheetAdapterError) as exc:
        raise ControllerPostprocessError(f"خروجی از دیالکت کنترلر خارج شد: {exc}", [str(exc)]) from exc

    inserted = list((serialized or {}).get("inserted_line_numbers") or [])
    if not inserted:
        raise ControllerPostprocessError(
            "ساختار خروجی تغییر کرده است ولی فهرست خط‌های درج‌شده گزارش نشد؛ خروجی مسدود شد.",
            ["inserted_line_numbers missing for a structural candidate"])
    if any(not isinstance(item, int) or item < 1 for item in inserted):
        raise ControllerPostprocessError(
            "شماره خط‌های درج‌شده نامعتبر است؛ خروجی مسدود شد.",
            ["inserted_line_numbers must be positive integers"])
    inserted_set = set(inserted)
    candidate_entries = []
    for index, raw in enumerate(resized_text.splitlines(), start=1):
        signature = _line_signature(raw)
        if signature is not None:
            candidate_entries.append((index, signature))
    source_entries = []
    for raw in source_text.splitlines():
        signature = _line_signature(raw)
        if signature is not None:
            source_entries.append(signature)
    pointer = 0
    skipped_inserted = 0
    for source_signature in source_entries:
        while pointer < len(candidate_entries) and candidate_entries[pointer][0] in inserted_set:
            skipped_inserted += 1
            pointer += 1
        if pointer >= len(candidate_entries):
            raise ControllerPostprocessError(
                "خروجی خط‌هایی از فایل پایه را از دست داده است؛ خروجی مسدود شد.",
                ["structural gate: candidate lost source lines"])
        candidate_index, candidate_signature = candidate_entries[pointer]
        if candidate_signature != source_signature:
            raise ControllerPostprocessError(
                f"تطابق خط‌به‌خط خراب شد (خط {candidate_index} خروجی)؛ خروجی مسدود شد.",
                [f"structural gate: line correspondence broken at candidate line {candidate_index}"])
        pointer += 1
    while pointer < len(candidate_entries) and candidate_entries[pointer][0] in inserted_set:
        skipped_inserted += 1
        pointer += 1
    if pointer < len(candidate_entries):
        raise ControllerPostprocessError(
            f"خروجی خط اضافهٔ حساب‌نشده دارد (خط {candidate_entries[pointer][0]})؛ خروجی مسدود شد.",
            [f"structural gate: unaccounted extra line at candidate line {candidate_entries[pointer][0]}"])
    if skipped_inserted != len(inserted_set):
        raise ControllerPostprocessError(
            "خط‌های درج‌شدهٔ گزارش‌شده در خروجی یافت نشدند؛ خروجی مسدود شد.",
            ["structural gate: declared insertions were not found in the candidate"])

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
        "gate": "structural_with_declared_insertions",
        "inserted_line_count": len(inserted_set),
    }
