"""Dialect-locked sheet postprocessor for the workshop's custom CNC controller.

The dialect was reverse-engineered from 65 production ``.cnc`` files supplied
by the machine owner (see ``CraftFlow_Gcode_Dialect_Analysis_FA.docx``):

* program start : ``%`` / ``G90 G54`` / ``G53 Z0`` / ``T#M6`` / ``M141`` /
  ``M6T#`` / ``G43 H#``  — ``M141`` (PLC one-shot start: vacuum / purge /
  ATC handshake) appears exactly once, only in the opening tool block.
* motion        : G0/G1/G2/G3, absolute X/Y (G90), incremental I/J arc
  centres. R-format arcs never occur and are rejected.
* tool switch   : mid-program ``M6T#`` + ``G43 H#`` (the first-stage
  ``T#M6`` select and ``M141`` never repeat after the header).
* per part end  : ``G0Z<clearance>`` rapid retract, then a ``G0X0Y0``
  program-zero park (stripped when nesting — the sheet parks once).
* program end   : ``G53`` machine park lines, ``M30``, ``%``. Files use
  CRLF line endings and ``N`` numbers stepping by 10.

The adapter accepts only sources that stay inside this whitelist; anything
else raises :class:`SheetAdapterError` and the planner falls back to the
blocked-output state (fail-closed). Every emitted program is numbered
``N10, N20, ...`` and terminated with the source's own ``G53`` park lines.
"""
from __future__ import annotations

from . import gcode_sheet

DIALECT_ADAPTER_KEY = "minimal_xy_router"

_DIALECT_G = frozenset({0, 1, 2, 3, 21, 43, 53, 54, 90})
_DIALECT_M = frozenset({3, 6, 30, 141})
_DIALECT_LETTERS = frozenset("GMXYZIJFSTH")
_PRE_M = frozenset({6, 141})
_BODY_G = frozenset({0, 1, 2, 3, 21, 43})
_BODY_M = frozenset({3, 6})
_POST_LETTERS = frozenset("GXYZ")
_N_STEP = 10
_EPS = 1e-6


class SheetAdapterError(ValueError):
    """Source or request is outside the verified controller dialect."""


def _words(line):
    return gcode_sheet._words(line)


def _letters(words):
    return [letter for letter, _value, _raw in words]


def _codes(words):
    g_codes, m_codes = [], []
    for letter, value, _raw in words:
        if letter == "G":
            if not value.is_integer():
                raise SheetAdapterError(f"کد G اعشاری در دیالکت کنترلر تعریف نشده: G{value:g}")
            g_codes.append(int(value))
        elif letter == "M":
            m_codes.append(int(value))
    return g_codes, m_codes


def _check_letters(line, words, allowed, section):
    stray = sorted({letter for letter, _v, _r in words if letter not in allowed})
    if stray:
        raise SheetAdapterError(
            f"خط خارج از دیالکت در {section} («{line}»): حروف {', '.join(stray)} مجاز نیستند."
        )
    residue = gcode_sheet.WORD_RE.sub("", line).strip()
    if residue:
        raise SheetAdapterError(f"خط خارج از دیالکت در {section} («{line}»): نویسهٔ قابل تفسیر «{residue}».")


def _tool_of(words):
    for letter, value, _raw in words:
        if letter == "T":
            if not value.is_integer() or value <= 0:
                raise SheetAdapterError(f"شماره ابزار نامعتبر است: T{value:g}")
            return int(value)
    return None


def _is_rapid_with(words, g_codes, m_codes, *, xy=False, z=False):
    if g_codes != [0] or any(code not in (0, 3) for code in m_codes):
        return False
    seq = _letters(words)
    if xy != ({"X", "Y"} <= set(seq)):
        return False
    if z != ("Z" in seq):
        return False
    return "I" not in seq and "J" not in seq and "H" not in seq and "T" not in seq and "F" not in seq


def _parse_preamble(pre):
    """Validate the opening tool block and extract (tool, h, m141_present)."""
    first_stage, second_stage, h_raw, h_count, m141 = [], [], None, 0, False
    for line in pre:
        words = _words(line)
        _check_letters(line, words, _DIALECT_LETTERS, "پری‌امبل")
        g_codes, m_codes = _codes(words)
        if any(code not in _DIALECT_G for code in g_codes):
            bad = ", ".join(f"G{code}" for code in g_codes if code not in _DIALECT_G)
            raise SheetAdapterError(f"کد G خارج از دیالکت در پری‌امبل («{line}»): {bad}")
        if any(code not in _PRE_M for code in m_codes):
            bad = ", ".join(f"M{code}" for code in m_codes if code not in _PRE_M)
            raise SheetAdapterError(f"کد M خارج از دیالکت در پری‌امبل («{line}»): {bad}")
        seq = _letters(words)
        if ("X" in seq or "Y" in seq) and 53 not in g_codes:
            raise SheetAdapterError(f"خط XY غیرمنتظره در پری‌امبل («{line}»).")
        if 53 in g_codes and ("X" in seq or "Y" in seq):
            raise SheetAdapterError(f"پارک ماشینِ دارای XY داخل پری‌امبل («{line}») در دیالکت دیده نشده است.")
        if "T" in seq and 6 not in m_codes:
            raise SheetAdapterError(f"انتخاب ابزار بدون M6 در پری‌امبل («{line}») مجاز نیست.")
        if "H" in seq and 43 not in g_codes:
            raise SheetAdapterError(f"ثبت H خارج از G43 در پری‌امبل («{line}») مجاز نیست.")
        if 43 in g_codes:
            h_count += 1
            h_raw = next((raw for letter, _v, raw in words if letter == "H"), None)
        if "T" in seq and 6 in m_codes:
            tool = _tool_of(words)
            if seq.index("T") < seq.index("M"):
                first_stage.append(tool)
            else:
                second_stage.append(tool)
        if 141 in m_codes:
            m141 = True
    if len(first_stage) != 1 or len(second_stage) != 1:
        raise SheetAdapterError(
            "پری‌امبل باید دقیقاً یک بلوک تعویض ابزار دومرحله‌ای (T#M6 و سپس M6T#) داشته باشد؛ "
            f"یافت‌شده: {len(first_stage)} و {len(second_stage)}."
        )
    if first_stage[0] != second_stage[0]:
        raise SheetAdapterError(
            f"شماره ابزار در دو مرحلهٔ تعویض تفاوت دارد: T{first_stage[0]} و T{second_stage[0]}."
        )
    if h_count != 1 or h_raw is None:
        raise SheetAdapterError(f"پری‌امبل باید دقیقاً یک G43 H# داشته باشد؛ یافت‌شده: {h_count}.")
    return first_stage[0], h_raw, m141


def _parse_body(body):
    """Validate motion body; return (body_lines_without_park, switches, retract_ok)."""
    switches = {}
    for index, line in enumerate(body):
        words = _words(line)
        _check_letters(line, words, _DIALECT_LETTERS, "بدنه")
        g_codes, m_codes = _codes(words)
        if any(code not in _BODY_G for code in g_codes):
            bad = ", ".join(f"G{code}" for code in g_codes if code not in _BODY_G)
            raise SheetAdapterError(f"کد G خارج از جای مجاز دیالکت در بدنه («{line}»): {bad}")
        if any(code not in _BODY_M for code in m_codes):
            bad = ", ".join(f"M{code}" for code in m_codes if code not in _BODY_M)
            raise SheetAdapterError(f"کد M خارج از جای مجاز دیالکت در بدنه («{line}»): {bad}")
        seq = _letters(words)
        if "T" in seq:
            if 6 not in m_codes or seq.index("T") < seq.index("M"):
                raise SheetAdapterError(
                    f"ابزار در بدنه فقط در قالب تعویض میانی M6T# مجاز است («{line}»)."
                )
        if 141 in m_codes:
            raise SheetAdapterError(f"M141 باید فقط یک‌بار در پری‌امبل بیاید («{line}»).")
        if "H" in seq and 43 not in g_codes:
            raise SheetAdapterError(f"ثبت H خارج از G43 در بدنه («{line}») مجاز نیست.")
        if 2 in g_codes or 3 in g_codes:
            if "R" in seq or "I" not in seq or "J" not in seq:
                raise SheetAdapterError(
                    f"قوس در این کنترلر فقط با مرکز I/J نوشته می‌شود؛ خط رد شد («{line}»)."
                )
        if "T" in seq:
            switches.setdefault(index, []).append(("tool", _tool_of(words)))
        if 43 in g_codes and "H" in seq:
            h_raw = next((raw for letter, _v, raw in words if letter == "H"), None)
            switches.setdefault(index, []).append(("h", h_raw))
    if body and _is_program_park(body[-1]):
        body = body[:-1]
    retract_ok = bool(body) and _is_retract(body[-1])
    return body, switches, retract_ok


def _is_program_park(line):
    words = _words(line)
    g_codes, m_codes = _codes(words)
    if not _is_rapid_with(words, g_codes, m_codes, xy=True, z=False):
        return False
    values = {letter: value for letter, value, _raw in words}
    return abs(values.get("X", 1.0)) <= _EPS and abs(values.get("Y", 1.0)) <= _EPS


def _is_retract(line):
    words = _words(line)
    g_codes, m_codes = _codes(words)
    return _is_rapid_with(words, g_codes, m_codes, xy=False, z=True)


def _parse_postamble(post):
    if not post:
        raise SheetAdapterError("فایل قطعه خط پارک ماشینی G53 در پایان ندارد؛ خروجی ورق مسدود شد.")
    for line in post:
        words = _words(line)
        _check_letters(line, words, _POST_LETTERS, "پایان‌بخش")
        g_codes, m_codes = _codes(words)
        if 53 not in g_codes or m_codes or any(code != 53 for code in g_codes):
            raise SheetAdapterError(
                f"پایان‌بخش فایل فقط باید حرکت پارک G53 باشد («{line}»)."
            )
    return list(post)


def analyze_piece(text):
    """Parse one part program into the verified dialect structure.

    Returns a dict with the tool block values, the body without its trailing
    program-zero park, per-line mid-body tool switches, and the ``G53`` park
    lines used verbatim as the sheet ender.
    """
    try:
        pre, body, post = gcode_sheet.split_program(text)
    except gcode_sheet.GcodeError as exc:
        raise SheetAdapterError(f"ساختار فایل قطعه قابل استفاده نیست: {exc}") from exc
    declared_mm = False
    for line in pre + body:
        for letter, value, _raw in _words(line):
            if letter == "G" and value.is_integer() and int(value) == 21:
                declared_mm = True
                break
    if not declared_mm:
        raise SheetAdapterError(
            "واحد فایل قطعه صریح نیست (G21 یافت نشد)؛ CNC_RESIZER_IMPLICIT_UNITS=mm را فعال کنید."
        )
    tool, h_raw, m141 = _parse_preamble(pre)
    body, switches, retract_ok = _parse_body(body)
    if not body:
        raise SheetAdapterError("بدنهٔ برش قطعه پس از حذف پارک خالی است.")
    if not retract_ok:
        raise SheetAdapterError(
            "آخرین خط بدنهٔ قطعه باید ارتفاع‌گیر سریع (G0 Z…) باشد تا جابه‌جایی بین قطعات ایمن بماند."
        )
    post = _parse_postamble(post)
    return {
        "tool": tool,
        "h": h_raw,
        "m141": m141,
        "body": body,
        "switches": switches,
        "post": post,
    }


def build_sheet_program(placements, adapter_key=DIALECT_ADAPTER_KEY):
    """Assemble one verified sheet program from placed parts.

    ``placements``: list of dicts with ``text``, ``place_x``, ``place_y``,
    ``rotated`` and ``name``.  The header (G90/G54, G53 retract, first tool
    block with a single ``M141``) is emitted once; a mid-sheet tool switch
    uses the controller's own mid-program form (``M6T#`` + ``G43 H#``).
    Every line is numbered with the dialect's ``N`` step of 10 and the file
    uses CRLF endings, exactly like the source programs.
    """
    if adapter_key != DIALECT_ADAPTER_KEY:
        raise SheetAdapterError(
            f"پست‌پروسسور ورق «{adapter_key}» در مخزن ثبت نشده است؛ کلید پشتیبانی‌شده: {DIALECT_ADAPTER_KEY}."
        )
    if not placements:
        raise SheetAdapterError("برای ساخت برنامه ورق حداقل یک قطعه لازم است.")
    cache = {}
    analyzed = []
    for item in placements:
        text = item["text"]
        if text not in cache:
            cache[text] = analyze_piece(text)
        analyzed.append((item, cache[text]))

    first_tool, first_h, first_m141 = cache[placements[0]["text"]]["tool"], cache[placements[0]["text"]]["h"], cache[placements[0]["text"]]["m141"]
    if not first_m141:
        raise SheetAdapterError(
            "نخستین قطعهٔ ورق M141 (شروع PLC کنترلر: وکیوم/هوا/دستوری ATC) ندارد؛ خروجی مسدود شد."
        )

    lines = ["%"]
    counter = {"n": 0}

    def emit(content):
        counter["n"] += _N_STEP
        lines.append(f"N{counter['n']}{content}")

    emit("G90G54")
    emit("G53Z0")
    emit(f"T{first_tool}M6")
    emit("M141")
    emit(f"M6T{first_tool}")
    emit(f"G43H{first_h}")
    active_tool, active_h = first_tool, first_h
    for item, structure in analyzed:
        if structure["tool"] != active_tool or structure["h"] != active_h:
            emit(f"M6T{structure['tool']}")
            emit(f"G43H{structure['h']}")
            active_tool, active_h = structure["tool"], structure["h"]
        bounds = gcode_sheet.source_bounds(structure["body"])
        moved = gcode_sheet.transform_body(
            structure["body"], bounds,
            float(item["place_x"]), float(item["place_y"]),
            bool(item.get("rotated", False)),
        )
        for index, line in enumerate(moved):
            emit(line.replace(" ", ""))
            for kind, value in structure["switches"].get(index, ()):
                if kind == "tool":
                    active_tool = value
                elif kind == "h":
                    active_h = value
    for line in cache[placements[0]["text"]]["post"]:
        emit(line.replace(" ", ""))
    emit("M30")
    lines.append("%")
    return "\r\n".join(lines) + "\r\n"
