"""Fail-closed line-patch serializer bridging engine candidates to the
verified workshop dialect postprocessor.

The engine preview deliberately never serializes G-code. Machine output for
an engine candidate is possible only as a *pure coordinate patch* of the
original source text: every source line is preserved byte-for-byte (header,
``N`` numbering, comments, blank lines, CRLF endings) except the X/Y (and,
defensively, I/J) words of moves whose resolved coordinates the preview
actually changed. The legacy ``controller.postprocess_resize`` dialect gate
then re-verifies the final text line-by-line (exact workshop dialect, M141
preamble, I/J-only arcs, unchanged header/body/post structure) before
anything reaches a machine.

Package-E phase three adds two structural extensions, both still strictly
fail-closed and both verified at runtime by the engine's reparse walk:

* ``block_copies`` — an ``array`` edit duplicates one source block's own
  line range verbatim right after that block, with every X/Y word that
  exists on a line translated by the copy delta (modal carries inside the
  copy chain work exactly like the original block). Copy lines are stripped
  of their ``N`` sequence word (N is optional per RS274 and duplicates would
  be ambiguous); the dialect gate ignores N. The result reports the final
  ``inserted_line_numbers`` so ``controller.postprocess_structural`` can
  prove the source lines' order and wording are preserved around them.
* ``arc_direction_flips`` — a ``mirror`` edit reverses every arc of the
  mirrored block, so the serializer accepts a ``G2 <-> G3`` swap (and the
  resulting I/J sign flip) on lines that carry an explicit G2/G3 word.
* ``rotated_move_ids`` — a ``rotate`` edit turns a block by 90°; rotation is
  orientation-preserving so G2/G3 and G41/G42 stay untouched, but a rotated
  line whose source carried only one axis word must end up explicit on both
  axes (the other coordinate changes too). For such declared moves the
  serializer INSERTS the missing word (``X``/``Y``, and for I/J arcs the
  missing centre offset) right after the last existing XY/I/J word of that
  line instead of blocking. Every other structural rule stays as strict as
  before.

Everything that cannot be expressed as such a patch is blocked here with a
precise reason list: incremental coordinates, machine-coordinate (G53)
patches, R-format arcs, non-XY arc planes, Z changes, changed
motion/feed/spindle/tool semantics, lost moves, inch sources, or a candidate
that does not belong to the parsed source.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from .parser import ParseError, parse_program

SERIALIZER_ADAPTER_KEY = "minimal_xy_router"

_NUMBER = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
_PATCH_RES = {letter: re.compile(rf"(?<![A-Z]){letter}{_NUMBER}", re.IGNORECASE) for letter in "XYIJ"}
# Mirror flips arc direction: the G word may swap between exactly 2 and 3.
# The lookahead keeps G20/G21/G2.5-style words out of the match.
_PATCH_RES["G"] = re.compile(r"(?<![A-Z])G([23])(?![.\d])", re.IGNORECASE)
_COMMENT_MARKS = ("(", ";")
_N_PREFIX_RE = re.compile(r"^\s*N\d+\s*")
_COPY_ALLOWED_M = frozenset({3, 7, 8})  # spindle/coolant-on: modal re-issue is harmless
_M_WORD_RE = re.compile(r"\bM\s*(\d+)", re.IGNORECASE)
_T_WORD_RE = re.compile(r"\bT\s*\d+", re.IGNORECASE)
_MAX_COPIES_PER_BLOCK = 64
_MAX_TOTAL_COPIES = 128


class SerializerBlocked(ValueError):
    """Candidate cannot be serialized as a pure coordinate patch."""

    def __init__(self, message, blockers=None):
        super().__init__(message)
        self.blockers = list(blockers or [message])


def _format_value(value: float) -> str:
    text = f"{value:.3f}"
    return "0.000" if text == "-0.000" else text


def _format_word(letter: str, value: float) -> str:
    return f"{int(round(value))}" if letter == "G" else _format_value(value)


def _candidate_moves(preview: dict[str, Any]) -> list[dict[str, Any]]:
    program_ir = preview.get("program_ir")
    if not isinstance(program_ir, dict):
        raise SerializerBlocked("Transformed intermediate representation is missing")
    moves = program_ir.get("moves")
    if not isinstance(moves, list):
        raise SerializerBlocked("Transformed intermediate representation has no moves")
    return moves


def _point_value(point: Any, axis: str) -> float | None:
    if point is None:
        return None
    return getattr(point, axis)


def _patch_plan(program: Any, preview: dict[str, Any]) -> dict[int, tuple[str, dict[str, float]]]:
    """Map source line index -> (raw_line, {word letter: new value}); fail closed."""
    blockers: list[str] = []
    validation = preview.get("validation") or {}
    if not validation.get("passed"):
        blockers.extend(validation.get("errors") or ["Geometry preview validation failed"])
    if preview.get("source_sha256") != program.source_sha256:
        blockers.append("Preview does not belong to this parsed source (sha256 mismatch)")
    if program.units == "inch":
        blockers.append("Explicit inch sources are normalized to mm in the IR; text patching would mix units")
    try:
        candidate_moves = _candidate_moves(preview)
    except SerializerBlocked as exc:
        blockers.extend(exc.blockers)
        candidate_moves = []
    if candidate_moves and len(candidate_moves) != len(program.moves):
        blockers.append("Candidate move count does not match the parsed source")
    if blockers:
        raise SerializerBlocked("Candidate cannot be serialized safely", blockers)

    source_moves = {move.id: move for move in program.moves}
    raw_flips = preview.get("arc_direction_flips") or []
    if not isinstance(raw_flips, list) or any(not isinstance(item, str) for item in raw_flips):
        blockers.append("arc_direction_flips must be a list of move ids")
        raw_flips = []
    arc_flips = set(raw_flips)
    raw_rotated = preview.get("rotated_move_ids") or []
    if not isinstance(raw_rotated, list) or any(not isinstance(item, str) for item in raw_rotated):
        blockers.append("rotated_move_ids must be a list of move ids")
        raw_rotated = []
    rotated_moves = set(raw_rotated)
    plan: dict[int, tuple[str, dict[str, float], dict[str, float]]] = {}
    for item in candidate_moves:
        move_id = item.get("id")
        source = source_moves.get(move_id)
        if source is None:
            blockers.append(f"Candidate move {move_id} does not exist in the source")
            continue
        is_rotated = move_id in rotated_moves
        motion_flipped = False
        if item.get("motion") != source.motion:
            if (move_id in arc_flips and source.arc is not None
                    and {source.motion, item.get("motion")} == {"arc_cw", "arc_ccw"}):
                motion_flipped = True  # declared mirror flip: G2 <-> G3
            else:
                blockers.append(f"Move {move_id} changed motion semantics")
        if (item.get("feed") != source.feed or item.get("spindle") != source.spindle
                or item.get("tool") != source.tool):
            blockers.append(f"Move {move_id} changed feed/spindle/tool semantics")
            continue
        start, end = item.get("start") or {}, item.get("end") or {}
        words: dict[str, float] = {}
        inserted: dict[str, float] = {}
        source_words = source.raw_words or {}
        # Absolute (G90) lines carry only the END coordinate as X/Y words; the
        # START is modal state resolved from the previous line. Patching from
        # the start would corrupt downward/sideways moves, so X/Y words are
        # taken exclusively from the candidate end point.
        for axis, letter in (("x", "X"), ("y", "Y")):
            before = _point_value(source.end, axis)
            after = end.get(axis)
            if before is None or after is None:
                if (before is None) != (after is None):
                    blockers.append(f"Move {move_id} end {axis} resolution changed")
            elif abs(before - after) > 1e-8:
                if letter.lower() in source_words or not is_rotated:
                    # Unchanged contract: a needed change lands in `words` and
                    # an absent word then fails at patch time ("cannot be
                    # patched unambiguously").
                    words[letter] = after
                else:
                    # Rotated blocks end up fully explicit: the missing axis
                    # word is inserted next to the existing ones.
                    inserted[letter] = after
        for point_name, candidate_point in (("start", start), ("end", end)):
            before = _point_value(getattr(source, point_name), "z")
            after = candidate_point.get("z")
            if before is None or after is None:
                if (before is None) != (after is None):
                    blockers.append(f"Move {move_id} {point_name} z resolution changed")
            elif abs(before - after) > 1e-8:
                blockers.append(f"Move {move_id} changed Z; only XY patching is supported")
        candidate_arc = item.get("arc") or {}
        if source.arc is not None:
            if candidate_arc.get("plane") != source.arc.plane:
                blockers.append(f"Move {move_id} changed arc plane")
            elif candidate_arc.get("plane") != "XY" and (words.get("X") is not None or words.get("Y") is not None):
                blockers.append(f"Move {move_id} is a non-XY arc; only XY arc patching is supported")
            elif "r" in (source.raw_words or {}):
                blockers.append(f"Move {move_id} is an R-format arc; the controller dialect uses I/J centres only")
            else:
                source_center = source.arc.center or {}
                candidate_center = candidate_arc.get("center") or {}
                for axis, letter in (("x", "I"), ("y", "J")):
                    candidate_start = start.get(axis)
                    before_center = source_center.get(axis)
                    after_center = candidate_center.get(axis)
                    if before_center is None or after_center is None or candidate_start is None:
                        continue
                    new_offset = after_center - candidate_start
                    old_offset = before_center - (_point_value(source.start, axis) or 0.0)
                    if abs(new_offset - old_offset) > 1e-8:
                        if letter.lower() in source_words:
                            words[letter] = new_offset
                        elif is_rotated:
                            inserted[letter] = new_offset
                        else:
                            blockers.append(f"Move {move_id} arc centre {letter} changed but the word is absent")
        elif candidate_arc:
            blockers.append(f"Move {move_id} gained arc geometry; semantics must not change")
        if motion_flipped:
            if letter_match := _PATCH_RES["G"].search(source.raw_line or ""):
                words["G"] = 3.0 if item.get("motion") == "arc_ccw" else 2.0
            else:
                blockers.append(
                    f"Move {move_id}: arc direction flip needs an explicit G2/G3 word on its line")
        if is_rotated and inserted and not any(
                letter.lower() in source_words for letter in "XYIJ"):
            blockers.append(
                f"Move {move_id}: a rotated line needs at least one existing X/Y/I/J word to "
                "anchor the inserted words next to")
        if not words and not inserted:
            continue  # untouched move: the line passes through byte-identical
        # Only moves that actually need a patch must be patchable:
        if source.coordinate_mode != "absolute":
            blockers.append(f"Move {move_id} is not absolute (G90); incremental patching is not supported")
        if source.machine_coordinates:
            blockers.append(f"Move {move_id} is a machine-coordinate (G53) move; patching is not supported")
        if not source.raw_line or any(mark in source.raw_line for mark in _COMMENT_MARKS):
            blockers.append(f"Move {move_id} source line cannot be patched safely (comment or missing text)")
            continue
        plan[source.source_line - 1] = (source.raw_line, words, inserted)
    if blockers:
        raise SerializerBlocked("Candidate cannot be serialized safely", blockers)
    return plan


def serialize_candidate(source_text: str, preview: dict[str, Any]) -> dict[str, Any]:
    """Patch the source text's own lines with the preview's changed XY words.

    Returns ``{"text", "changed_move_count", "changed_line_numbers",
    "source_sha256", "candidate_sha256", "serializer_adapter_key"}``. Raises
    :class:`SerializerBlocked` when the candidate is not a pure coordinate
    patch; no partial output is ever returned.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        raise SerializerBlocked("Source text is empty")
    try:
        program = parse_program(source_text)
    except ParseError as exc:
        raise SerializerBlocked(f"Source text does not parse: {exc}") from exc
    plan = _patch_plan(program, preview)
    lines = source_text.splitlines(True)
    changed_lines: list[int] = []
    for line_index, (raw_line, words, insertions) in plan.items():
        if not 0 <= line_index < len(lines):
            raise SerializerBlocked(f"Source line {line_index + 1} is out of range")
        raw = lines[line_index]
        eol = "\r\n" if raw.endswith("\r\n") else ("\n" if raw.endswith("\n") else "")
        body = raw[: len(raw) - len(eol)]
        if body != raw_line:
            raise SerializerBlocked(f"Source line {line_index + 1} changed since parsing")
        patched = body
        for letter, value in words.items():
            matches = list(_PATCH_RES[letter].finditer(patched))
            if len(matches) != 1:
                raise SerializerBlocked(
                    f"Source line {line_index + 1}: word {letter} cannot be patched unambiguously")
            match = matches[0]
            patched = patched[:match.start(1)] + _format_word(letter, value) + patched[match.end(1):]
        if insertions:
            # A rotated line must end up explicit on both axes: the missing
            # words are inserted right after the last existing X/Y/I/J word so
            # the line's own dialect style (spaces or compact) is preserved.
            anchor = None
            for letter in "XYIJ":
                for match in _PATCH_RES[letter].finditer(patched):
                    if anchor is None or match.end() > anchor:
                        anchor = match.end()
            if anchor is None:  # guarded in _patch_plan; defense in depth
                raise SerializerBlocked(
                    f"Source line {line_index + 1}: no existing X/Y/I/J word to anchor the inserted words")
            text = "".join(f"{letter}{_format_word(letter, value)}"
                           for letter, value in sorted(insertions.items()))
            patched = patched[:anchor] + text + patched[anchor:]
        lines[line_index] = patched + eol
        changed_lines.append(line_index + 1)
    insertions, copy_move_count = _copy_plan(program, preview, source_text)
    if insertions:
        grouped: dict[int, list[str]] = {}
        for position, new_lines in insertions:
            grouped.setdefault(position, []).extend(new_lines)
        entries: list[list[Any]] = [[text, False] for text in lines]
        # bugfix: an unterminated FINAL source line glues onto the first copy
        # inserted after it ("...J0" + "G0 X160…"). The copies carry their own
        # EOL (see _copy_plan), so the source line itself must be terminated
        # with the file's own EOL before anything is appended after it — a
        # formatting repair only, never a structural change.
        file_eol = "\r\n" if "\r\n" in source_text else "\n"
        if entries and not entries[-1][0].endswith(("\r\n", "\n")):
            entries[-1][0] += file_eol
        for position in sorted(grouped, reverse=True):
            # ``position`` is the 1-based source line the copy follows
            # (block.ended_at_line); the insertion slice index equals it.
            if not 0 <= position <= len(entries):
                raise SerializerBlocked(f"Copy insertion point {position + 1} is out of range")
            entries[position:position] = [[line, True] for line in grouped[position]]
        lines = entries
    else:
        lines = [[text, False] for text in lines]
    text = "".join(entry[0] for entry in lines)
    inserted_line_numbers = [index + 1 for index, entry in enumerate(lines) if entry[1]]
    return {
        "text": text,
        "changed_move_count": len(plan) + copy_move_count,
        "changed_line_numbers": sorted(changed_lines),
        "inserted_line_numbers": inserted_line_numbers,
        "inserted_line_count": len(inserted_line_numbers),
        "source_sha256": program.source_sha256,
        "candidate_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "serializer_adapter_key": SERIALIZER_ADAPTER_KEY,
    }


def _validate_copy_block(block: Any, label: str) -> None:
    """Structural guards for a block whose text will be duplicated."""
    motions = block.motions
    cut = [move for move in motions if move.motion in ("linear", "arc_cw", "arc_ccw")]
    if not cut:
        raise SerializerBlocked(f"{label}: the block has no cutting motion to copy")
    entry = motions[0]
    if entry.motion != "rapid" or entry.machine_coordinates:
        raise SerializerBlocked(
            f"{label}: the block does not start with an independent positioning rapid")
    entry_words = entry.raw_words or {}
    if "x" not in entry_words or "y" not in entry_words:
        raise SerializerBlocked(
            f"{label}: the entry rapid must carry both X and Y words so the copy's "
            "positioning does not depend on modal state from outside the copy")
    if entry.end is None or entry.end.x is None or entry.end.y is None:
        raise SerializerBlocked(f"{label}: the positioning rapid has unresolved coordinates")
    first = cut[0]
    if (first.start is None or first.start.x is None or first.start.y is None
            or abs(entry.end.x - first.start.x) > 1e-6
            or abs(entry.end.y - first.start.y) > 1e-6):
        raise SerializerBlocked(
            f"{label}: the positioning rapid does not end at the first cutting move")
    for move in motions:
        raw_line = move.raw_line or ""
        if _T_WORD_RE.search(raw_line):
            raise SerializerBlocked(f"{label}: the block contains a tool word; duplicating tool calls is not supported")
        for code in _M_WORD_RE.findall(raw_line):
            if int(code) not in _COPY_ALLOWED_M:
                raise SerializerBlocked(
                    f"{label}: the block contains M{code}; duplicating this mode-tearing M-code is not supported")
        if move.coordinate_mode != "absolute":
            raise SerializerBlocked(f"{label}: move {move.id} is not absolute (G90)")
        if move.machine_coordinates:
            raise SerializerBlocked(f"{label}: move {move.id} is a machine-coordinate (G53) move")
        if move.motion == "rapid" and move is not entry:
            if "x" in (move.raw_words or {}) or "y" in (move.raw_words or {}):
                raise SerializerBlocked(
                    f"{label}: an internal repositioning rapid is not supported; "
                    "blocks are expected to contain Z-only rapids only")


def _patch_copy_line(body: str, move: Any, dx: float, dy: float, label: str, line_no: int) -> str:
    """Translate the X/Y words that exist on one copied line by (dx, dy)."""
    raw_words = move.raw_words or {}
    for axis, letter in (("x", "X"), ("y", "Y")):
        delta = dx if axis == "x" else dy
        if axis not in raw_words:
            continue  # modal carry inside the copy chain
        end = getattr(move.end, axis, None) if move.end is not None else None
        if end is None:
            raise SerializerBlocked(f"{label}: line {line_no} has an unresolved {axis.upper()} end")
        matches = list(_PATCH_RES[letter].finditer(body))
        if len(matches) != 1:
            raise SerializerBlocked(
                f"{label}: line {line_no}: word {letter} cannot be patched unambiguously")
        match = matches[0]
        body = body[:match.start(1)] + _format_value(end + delta) + body[match.end(1):]
    return body


def _copy_plan(program: Any, preview: dict[str, Any],
               source_text: str) -> tuple[list[tuple[int, list[str]]], int]:
    """Build the text-duplication plan for ``block_copies`` (array edits).

    Returns ``([(insert_after_zero_based, [raw_lines...]), ...], copied_move_count)``.
    """
    copies = preview.get("block_copies")
    if not copies:
        return [], 0
    if not isinstance(copies, list):
        raise SerializerBlocked("block_copies must be a list")
    block_by_id = {block.id: block for block in program.blocks}
    lines = source_text.splitlines(True)
    insertions: list[tuple[int, list[str]]] = []
    seen_blocks: set[str] = set()
    total_copies = 0
    copied_move_count = 0
    for group_index, group in enumerate(copies):
        label = f"Copy group {group_index + 1}"
        if not isinstance(group, dict) or not isinstance(group.get("source_block_id"), str):
            raise SerializerBlocked(f"{label}: source_block_id must be a string")
        block_id = group["source_block_id"]
        block = block_by_id.get(block_id)
        if block is None:
            raise SerializerBlocked(f"{label}: unknown source block {block_id!r}")
        if block_id in seen_blocks:
            raise SerializerBlocked(f"{label}: block {block_id} is duplicated across copy groups")
        seen_blocks.add(block_id)
        deltas = group.get("deltas")
        if not isinstance(deltas, list) or not deltas:
            raise SerializerBlocked(f"{label}: deltas must be a non-empty list")
        if len(deltas) > _MAX_COPIES_PER_BLOCK:
            raise SerializerBlocked(
                f"{label}: at most {_MAX_COPIES_PER_BLOCK} copies per block are supported")
        total_copies += len(deltas)
        if total_copies > _MAX_TOTAL_COPIES:
            raise SerializerBlocked(
                f"At most {_MAX_TOTAL_COPIES} copies in total are supported")
        _validate_copy_block(block, label)
        moves_by_line = {move.source_line: move for move in block.motions}
        copied_move_count += len(block.motions) * len(deltas)
        for delta_index, delta in enumerate(deltas):
            delta_label = f"{label} copy {delta_index + 1}"
            if not isinstance(delta, dict):
                raise SerializerBlocked(f"{delta_label}: delta must be an object")
            dx = _finite_delta(delta.get("dx"), f"{delta_label} dx")
            dy = _finite_delta(delta.get("dy"), f"{delta_label} dy")
            if dx == 0 and dy == 0:
                raise SerializerBlocked(f"{delta_label}: a zero-delta copy would recut the same path")
            new_lines: list[str] = []
            for line_no in range(block.started_by_line, block.ended_at_line + 1):
                if not 0 <= line_no - 1 < len(lines):
                    raise SerializerBlocked(f"{delta_label}: source line {line_no} is out of range")
                raw = lines[line_no - 1]
                eol = "\r\n" if raw.endswith("\r\n") else ("\n" if raw.endswith("\n") else "")
                if not eol:
                    # bugfix: an unterminated LAST source line used to produce
                    # unterminated copy lines that glued onto whatever follows.
                    # Copies are new text, so they always get the file's own EOL.
                    eol = "\r\n" if "\r\n" in source_text else "\n"
                body = _N_PREFIX_RE.sub("", raw[: len(raw) - len(eol) if raw.endswith(("\r\n", "\n")) else len(raw)])
                move = moves_by_line.get(line_no)
                if move is not None:
                    body = _patch_copy_line(body, move, dx, dy, delta_label, line_no)
                new_lines.append(body + eol)
            insertions.append((block.ended_at_line, new_lines))
    return insertions, copied_move_count


def _finite_delta(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SerializerBlocked(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise SerializerBlocked(f"{label} must be finite")
    return number
