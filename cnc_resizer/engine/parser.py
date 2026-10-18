"""Conservative modal G-code parser that preserves source traceability.

Coordinates remain in program units when G20/G21 are absent. The parser records
unknown controller commands and never decides whether a parsed program is safe
for machine execution.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from .ir import ArcGeometry, Motion, PathBlock, Point3, Program

WORD_RE = re.compile(r"([A-Z])([+-]?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)
G_RE = re.compile(r"G([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)
M_RE = re.compile(r"M(\d+)", re.IGNORECASE)
COMPACT_M_T_RE = re.compile(r"(?<![A-Z])M\d+T\d+(?!\d)", re.IGNORECASE)
UNSUPPORTED_CONSTRUCT_RE = re.compile(r"#(?:[A-Z0-9_]+|<[^>]+>)|\[[^\]]*\]|\b(?:IF|WHILE|GOTO|CALL|SUB|ENDSUB|ENDM)\b", re.IGNORECASE)
PAREN_COMMENT_RE = re.compile(r"\([^)]*\)")
KNOWN_M = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 30}
SUPPORTED_G = {0, 1, 2, 3, 17, 18, 19, 20, 21, 40, 43, 49, 53, 54, 55, 56, 57, 58, 59, 80, 90, 91, 94, 95}
MAX_MOVES = 100_000


class ParseError(ValueError):
    """Malformed or ambiguous source program; no output may be generated."""


def _strip_comments(raw: str) -> str:
    return PAREN_COMMENT_RE.sub(" ", raw.split(";", 1)[0]).upper()


def _point(position: dict[str, float], known: dict[str, bool]) -> Point3 | None:
    if not (known["X"] and known["Y"]):
        return None
    return Point3(position["X"], position["Y"], position["Z"] if known["Z"] else None)


def _sweep(start: Point3, end: Point3, center: dict[str, float], axes: tuple[str, str], clockwise: bool) -> float:
    first, second = axes
    a0 = math.atan2(getattr(start, second) - center[second], getattr(start, first) - center[first])
    a1 = math.atan2(getattr(end, second) - center[second], getattr(end, first) - center[first])
    sweep = -((a0 - a1) % (2 * math.pi)) if clockwise else (a1 - a0) % (2 * math.pi)
    if abs(sweep) < 1e-9:
        sweep = -2 * math.pi if clockwise else 2 * math.pi
    return sweep


def _arc_geometry(motion: int, plane: str | None, start: Point3 | None, end: Point3 | None,
                  words: dict[str, float], scale: float) -> ArcGeometry:
    direction = "clockwise" if motion == 2 else "counterclockwise"
    offsets: dict[str, float] = {key.lower(): value * scale for key, value in words.items() if key in "IJK"}
    plane_map = {"XY": ("x", "y", "i", "j"), "XZ": ("x", "z", "i", "k"), "YZ": ("y", "z", "j", "k")}
    if plane not in plane_map or start is None or end is None:
        return ArcGeometry(plane, direction, None, None, None, None, offsets)
    first, second, offset_first, offset_second = plane_map[plane]
    if offset_first not in offsets or offset_second not in offsets:
        return ArcGeometry(plane, direction, None, None, None, None, offsets)
    if getattr(start, first) is None or getattr(start, second) is None or getattr(end, first) is None or getattr(end, second) is None:
        return ArcGeometry(plane, direction, None, None, None, None, offsets)
    center = {first: getattr(start, first) + offsets[offset_first],
              second: getattr(start, second) + offsets[offset_second]}
    r0 = math.hypot(getattr(start, first) - center[first], getattr(start, second) - center[second])
    r1 = math.hypot(getattr(end, first) - center[first], getattr(end, second) - center[second])
    valid = math.isfinite(r0) and r0 > 0 and abs(r0 - r1) <= max(0.05, r0 * 1e-4)
    sweep = _sweep(start, end, center, (first, second), motion == 2)
    return ArcGeometry(plane, direction, center, r0, sweep, valid, offsets)


def parse_program(text: str, *, filename: str = "<memory>") -> Program:
    if not isinstance(text, str) or not text.strip():
        raise ParseError("G-code source is empty")
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) > 2 * 1024 * 1024:
        raise ParseError("G-code source exceeds 2 MiB")
    lines = text.splitlines()
    if not any(G_RE.search(_strip_comments(line)) for line in lines):
        raise ParseError("No G-code words found")

    mode: str | None = None
    units: str | None = None
    plane: str | None = None
    coordinate_system: str | None = None
    motion_mode: int | None = None
    tool: int | None = None
    feed: float | None = None
    spindle: float | None = None
    work_pos = {axis: 0.0 for axis in "XYZ"}
    machine_pos = {axis: 0.0 for axis in "XYZ"}
    work_known = {axis: False for axis in "XYZ"}
    machine_known = {axis: False for axis in "XYZ"}
    unit_scale = 1.0
    motion_seen = False
    explicit_units_seen = False
    moves: list[Motion] = []
    blocks: list[PathBlock] = []
    active_block: PathBlock | None = None
    tools: set[int] = set()
    h_registers: list[dict[str, Any]] = []
    feeds: set[float] = set()
    spindles: set[float] = set()
    unknown_g: dict[str, set[int]] = {}
    unknown_m: dict[str, set[int]] = {}
    compact_tokens: dict[str, set[int]] = {}
    unsupported_constructs: list[dict[str, Any]] = []
    warnings: list[str] = []
    observed_g: Counter[str] = Counter()
    observed_m: Counter[str] = Counter()

    for line_no, raw in enumerate(lines, 1):
        code = _strip_comments(raw)
        if not code.strip() or code.strip() == "%":
            continue
        for token in UNSUPPORTED_CONSTRUCT_RE.findall(code):
            unsupported_constructs.append({"token": token, "source_line": line_no})
        word_pairs = [(letter.upper(), float(value)) for letter, value in WORD_RE.findall(code)]
        words: dict[str, float] = {letter: value for letter, value in word_pairs}
        g_values = [float(value) for value in G_RE.findall(code)]
        integer_g = [int(value) for value in g_values if value.is_integer()]
        m_values = [int(value) for value in M_RE.findall(code)]
        for value in m_values:
            observed_m[f"M{value}"] += 1
        motion_codes = [value for value in integer_g if value in (0, 1, 2, 3)]
        if len(set(motion_codes)) > 1:
            raise ParseError(f"Conflicting motion modes on line {line_no}")
        if 90 in integer_g and 91 in integer_g:
            raise ParseError(f"Conflicting coordinate modes on line {line_no}")

        for value in g_values:
            label = f"G{int(value) if value.is_integer() else format(value, 'g')}"
            observed_g[label] += 1
            if not value.is_integer():
                unknown_g.setdefault(label, set()).add(line_no)
                continue
            g = int(value)
            if g not in SUPPORTED_G:
                unknown_g.setdefault(label, set()).add(line_no)
            if g == 90:
                mode = "absolute"
            elif g == 91:
                mode = "incremental"
            elif g == 17:
                plane = "XY"
            elif g == 18:
                plane = "XZ"
            elif g == 19:
                plane = "YZ"
            elif g == 20:
                if motion_seen and not explicit_units_seen:
                    warnings.append(f"Unit system becomes explicit only after motion at line {line_no}; earlier positions remain ambiguous.")
                units, unit_scale, explicit_units_seen = "inch", 25.4, True
            elif g == 21:
                if motion_seen and not explicit_units_seen:
                    warnings.append(f"Unit system becomes explicit only after motion at line {line_no}; earlier positions remain ambiguous.")
                units, unit_scale, explicit_units_seen = "mm", 1.0, True
            elif 54 <= g <= 59:
                coordinate_system = f"G{g}"
            if g in (0, 1, 2, 3):
                motion_mode = g

        for value in m_values:
            if value not in KNOWN_M:
                unknown_m.setdefault(f"M{value}", set()).add(line_no)
        for match in COMPACT_M_T_RE.finditer(code):
            compact_tokens.setdefault(match.group(0), set()).add(line_no)
        if "T" in words and words["T"].is_integer():
            tool = int(words["T"])
            tools.add(tool)
        if "H" in words:
            h_registers.append({"value": words["H"], "source_line": line_no})
        if "F" in words:
            feed = words["F"]
            feeds.add(feed)
        if "S" in words:
            spindle = words["S"]
            spindles.add(spindle)

        axis_words = {axis: words[axis] * unit_scale for axis in "XYZ" if axis in words}
        if axis_words and motion_mode is None:
            warnings.append(f"Axis coordinates without an active motion mode at line {line_no} were not interpreted.")
        if not axis_words or motion_mode is None:
            continue
        if len(moves) >= MAX_MOVES:
            raise ParseError(f"Program exceeds the {MAX_MOVES}-move safety limit")
        motion_seen = True
        machine_coordinates = 53 in integer_g
        position = machine_pos if machine_coordinates else work_pos
        known = machine_known if machine_coordinates else work_known
        start = _point(position, known)
        for axis, value in axis_words.items():
            if mode == "absolute":
                position[axis] = value
                known[axis] = True
            elif mode == "incremental" and known[axis]:
                position[axis] += value
            else:
                known[axis] = False
        end = _point(position, known)
        motion_name = {0: "rapid", 1: "linear", 2: "arc_cw", 3: "arc_ccw"}[motion_mode]
        arc = _arc_geometry(motion_mode, plane or "XY", start, end, words, unit_scale) if motion_mode in (2, 3) else None
        if arc is not None and plane is None:
            arc.plane_assumed = True
        move = Motion(
            id=f"move-{len(moves)+1:06d}", source_line=line_no, motion=motion_name,
            start=start, end=end, feed=feed, spindle=spindle, tool=tool,
            h_offset=words.get("H"), coordinate_mode=mode, coordinate_system=coordinate_system,
            machine_coordinates=machine_coordinates,
            arc=arc, raw_words={letter.lower(): value for letter, value in words.items() if letter in "XYZIJKRFSTH"},
            raw_line=raw,
        )
        moves.append(move)
        if machine_coordinates:
            continue
        if motion_mode == 0 and ("X" in axis_words or "Y" in axis_words):
            active_block = PathBlock(
                id=f"block-{len(blocks)+1:04d}", started_by_line=line_no, ended_at_line=line_no,
                start=end, motions=[], machine_coordinates=False,
            )
            blocks.append(active_block)
        elif active_block is None:
            active_block = PathBlock(
                id=f"block-{len(blocks)+1:04d}", started_by_line=line_no, ended_at_line=line_no,
                start=start or end, motions=[], machine_coordinates=False,
            )
            blocks.append(active_block)
        active_block.ended_at_line = line_no
        move.source_block = active_block.id
        active_block.motions.append(move)

    if units is None:
        warnings.append("Units are not declared with G20/G21; coordinates remain in unspecified program units.")
    if coordinate_system is not None:
        warnings.append("Work coordinate offset is recorded by name only; no machine-specific G54-G59 offset is applied.")
    if any(move.machine_coordinates for move in moves):
        warnings.append("G53 machine-coordinate moves are preserved in the IR but excluded from work-coordinate feature geometry.")
    if any(code == "G43" or code.startswith("G43.") for code in observed_g):
        warnings.append("Tool-length compensation is recorded but not applied to XYZ geometry.")
    if plane is None and any(move.arc for move in moves):
        warnings.append("No plane-selection code was present; G17/XY was assumed for geometric preview only.")
    if any(move.arc and move.arc.valid is not True for move in moves):
        warnings.append("One or more arcs lack a supported center definition or have inconsistent radii.")
    if unknown_m:
        warnings.append("Unknown/controller-specific M codes are present; a controller profile is required.")
    if unknown_g:
        warnings.append("Unknown or unsupported G codes are present; a controller profile is required.")
    if compact_tokens:
        warnings.append("Compact M/T command forms were preserved as controller-specific tokens.")
    if unsupported_constructs:
        warnings.append("Macro/expression/control-flow constructs were preserved but not expanded into static geometry.")
    if not any(move.motion in ("linear", "arc_cw", "arc_ccw") for move in moves):
        warnings.append("No cutting/interpolation motions were found.")
    return Program(
        filename=filename, source_sha256=hashlib.sha256(raw_bytes).hexdigest(), source_bytes=len(raw_bytes),
        line_count=len(lines), units=units, coordinate_mode=mode, plane=plane,
        coordinate_system=coordinate_system, tools=sorted(tools), feed_values=sorted(feeds),
        h_registers=h_registers,
        observed_g_codes=sorted(observed_g), observed_m_codes=sorted(observed_m),
        spindle_values=sorted(spindles), moves=moves, blocks=blocks,
        unknown_g_codes=[{"code": code, "source_lines": sorted(source_lines)} for code, source_lines in sorted(unknown_g.items())],
        unknown_m_codes=[{"code": code, "source_lines": sorted(source_lines)} for code, source_lines in sorted(unknown_m.items())],
        compact_controller_tokens=[{"token": token, "source_lines": sorted(source_lines)} for token, source_lines in sorted(compact_tokens.items())],
        unsupported_constructs=unsupported_constructs,
        warnings=warnings,
    )
