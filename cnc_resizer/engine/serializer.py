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

Everything that cannot be expressed as such a patch is blocked here with a
precise reason list: incremental coordinates, machine-coordinate (G53)
patches, R-format arcs, non-XY arc planes, Z changes, changed
motion/feed/spindle/tool semantics, lost moves, inch sources, or a candidate
that does not belong to the parsed source.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .parser import ParseError, parse_program

SERIALIZER_ADAPTER_KEY = "minimal_xy_router"

_NUMBER = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
_PATCH_RES = {letter: re.compile(rf"(?<![A-Z]){letter}{_NUMBER}", re.IGNORECASE) for letter in "XYIJ"}
_COMMENT_MARKS = ("(", ";")


class SerializerBlocked(ValueError):
    """Candidate cannot be serialized as a pure coordinate patch."""

    def __init__(self, message, blockers=None):
        super().__init__(message)
        self.blockers = list(blockers or [message])


def _format_value(value: float) -> str:
    text = f"{value:.3f}"
    return "0.000" if text == "-0.000" else text


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
    plan: dict[int, tuple[str, dict[str, float]]] = {}
    for item in candidate_moves:
        move_id = item.get("id")
        source = source_moves.get(move_id)
        if source is None:
            blockers.append(f"Candidate move {move_id} does not exist in the source")
            continue
        if (item.get("motion") != source.motion or item.get("feed") != source.feed
                or item.get("spindle") != source.spindle or item.get("tool") != source.tool):
            blockers.append(f"Move {move_id} changed motion/feed/spindle/tool semantics")
            continue
        start, end = item.get("start") or {}, item.get("end") or {}
        words: dict[str, float] = {}
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
                words[letter] = after
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
                        if letter not in (source.raw_words or {}):
                            blockers.append(f"Move {move_id} arc centre {letter} changed but the word is absent")
                        else:
                            words[letter] = new_offset
        elif candidate_arc:
            blockers.append(f"Move {move_id} gained arc geometry; semantics must not change")
        if not words:
            continue  # untouched move: the line passes through byte-identical
        # Only moves that actually need a patch must be patchable:
        if source.coordinate_mode != "absolute":
            blockers.append(f"Move {move_id} is not absolute (G90); incremental patching is not supported")
        if source.machine_coordinates:
            blockers.append(f"Move {move_id} is a machine-coordinate (G53) move; patching is not supported")
        if not source.raw_line or any(mark in source.raw_line for mark in _COMMENT_MARKS):
            blockers.append(f"Move {move_id} source line cannot be patched safely (comment or missing text)")
            continue
        plan[source.source_line - 1] = (source.raw_line, words)
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
    for line_index, (raw_line, words) in plan.items():
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
            patched = patched[:match.start(1)] + _format_value(value) + patched[match.end(1):]
        lines[line_index] = patched + eol
        changed_lines.append(line_index + 1)
    text = "".join(lines)
    return {
        "text": text,
        "changed_move_count": len(plan),
        "changed_line_numbers": sorted(changed_lines),
        "source_sha256": program.source_sha256,
        "candidate_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "serializer_adapter_key": SERIALIZER_ADAPTER_KEY,
    }
