"""Conservative, read-only G-code diagnostics for reverse-engineering stage one.

This module does not modify the source program and does not generate executable
G-code. Coordinates and geometry are reported as observations in program units;
controller semantics and physical machining intent are deliberately not inferred.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import core

_WORD_RE = re.compile(r"([A-Z])([+-]?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)
_G_RE = re.compile(r"G([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)
_M_RE = re.compile(r"M(\d+)", re.IGNORECASE)
_TOOL_RE = re.compile(r"T(\d+)", re.IGNORECASE)
_COMPACT_M_T_RE = re.compile(r"(?<![A-Z])M\d+T\d+(?!\d)", re.IGNORECASE)
_COMMENT_RE = re.compile(r"\([^)]*\)")

# Common, widely used M codes. This is intentionally not a controller dialect.
_KNOWN_M_CODES = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 30, 48, 49, 98, 99}
_SUPPORTED_G_CODES = {
    0, 1, 2, 3, 17, 18, 19, 20, 21, 40, 43, 49, 53,
    54, 55, 56, 57, 58, 59, 80, 90, 91, 94, 95,
}


def _clean_code(raw: str) -> str:
    return _COMMENT_RE.sub(" ", raw.split(";", 1)[0]).upper()


def _number(value: str) -> float:
    return float(value)


def _rounded(value: float | None, places: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, places)


def _classify_block(block: dict[str, Any]) -> dict[str, Any]:
    if not any(move["motion"] in ("G1", "G2", "G3") for move in block["moves"]):
        return {"candidate": "rapid_positioning_only", "confidence": "high_motion_type_only",
                "evidence": ["block contains no G1/G2/G3 cutting/interpolation moves"]}
    bounds = block.get("bounds")
    if not bounds:
        return {"candidate": "unresolved_geometry", "confidence": "none", "evidence": ["XY positions could not be resolved"]}
    width = bounds["x_max"] - bounds["x_min"]
    height = bounds["y_max"] - bounds["y_min"]
    moves = block["moves"]
    arcs = [move for move in moves if move["motion"] in ("G2", "G3")]
    closed = bool(block.get("closed_by_endpoints"))
    if closed and arcs and width <= 5 and height <= 5:
        return {"candidate": "small_closed_loop_or_hole", "confidence": "high_geometry_only",
                "evidence": ["closed path", "arc motion present", "endpoint bounds <= 5 program units"]}
    if closed and arcs and width > 100 and height > 100:
        return {"candidate": "large_closed_contour_candidate", "confidence": "medium_geometry_only",
                "evidence": ["path returns near its start", "arc motion present", "large XY bounds", "not proof of finished-part boundary"]}

    long_x = []
    for move in moves:
        if move["motion"] != "G1" or not move.get("start") or not move.get("end"):
            continue
        dx = move["end"]["x"] - move["start"]["x"]
        dy = move["end"]["y"] - move["start"]["y"]
        if abs(dx) >= 50 and abs(dy) <= max(15.0, abs(dx) * 0.03):
            long_x.append(1 if dx > 0 else -1)
    alternations = sum(a != b for a, b in zip(long_x, long_x[1:]))
    alternation_ratio = alternations / max(1, len(long_x) - 1)
    if width > 100 and len(long_x) >= 4 and alternation_ratio >= 0.6:
        return {"candidate": "zigzag_or_raster_path", "confidence": "medium_geometric_pattern",
                "evidence": [f"{len(long_x)} long near-horizontal passes", f"direction alternation ratio {alternation_ratio:.2f}"]}
    if max(width, height) > 100 and min(width, height) <= 20:
        return {"candidate": "elongated_groove_or_edge_path", "confidence": "low_geometric_pattern",
                "evidence": ["large aspect ratio in endpoint bounds", "physical operation is unconfirmed"]}
    if closed and width > 100 and height > 100:
        return {"candidate": "large_closed_contour_candidate", "confidence": "medium_geometry_only",
                "evidence": ["path returns near its start", "large XY bounds", "not proof of finished-part boundary"]}
    if width > 100 and height > 100:
        return {"candidate": "large_area_or_open_path", "confidence": "low_geometric_pattern",
                "evidence": ["large XY bounds", "not enough evidence to call an outer contour"]}
    return {"candidate": "unclassified_toolpath", "confidence": "low", "evidence": ["no stage-one geometric rule matched"]}


def analyze_text(text: str, *, filename: str = "<memory>", source_sha256: str | None = None) -> dict[str, Any]:
    """Return a JSON-serializable diagnostic report without modifying ``text``."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    raw_bytes = text.encode("utf-8")
    digest = source_sha256 or hashlib.sha256(raw_bytes).hexdigest()
    lines = text.splitlines()

    coordinate_mode: str | None = None
    units: str | None = None
    plane: str | None = None
    motion_mode: int | None = None
    position = {axis: 0.0 for axis in "XYZ"}
    known_position = {axis: False for axis in "XYZ"}
    current_tool: int | None = None
    current_feed: float | None = None
    current_spindle: float | None = None
    blocks: list[dict[str, Any]] = []
    current_block: dict[str, Any] | None = None
    all_moves: list[dict[str, Any]] = []
    unknown_m: dict[str, set[int]] = defaultdict(set)
    compact_m_t: dict[str, set[int]] = defaultdict(set)
    unknown_g: dict[str, set[int]] = defaultdict(set)
    observed_g: Counter[str] = Counter()
    observed_m: Counter[str] = Counter()
    tools: dict[int, set[int]] = defaultdict(set)
    feeds: dict[str, set[int]] = defaultdict(set)
    spindles: dict[str, set[int]] = defaultdict(set)
    coordinate_modes: Counter[str] = Counter()
    unit_modes: Counter[str] = Counter()
    planes: Counter[str] = Counter()
    machine_coordinate_lines: set[int] = set()

    for line_no, raw in enumerate(lines, 1):
        code = _clean_code(raw)
        if not code.strip() or code.strip() == "%":
            continue
        words = [(letter.upper(), _number(value)) for letter, value in _WORD_RE.findall(code)]
        g_numbers = [float(value) for value in _G_RE.findall(code)]
        g_values = [int(value) for value in g_numbers if value.is_integer()]
        m_values = [int(value) for value in _M_RE.findall(code)]
        for value in g_numbers:
            label = f"G{int(value) if value.is_integer() else format(value, 'g')}"
            observed_g[label] += 1
            if not value.is_integer():
                unknown_g[label].add(line_no)
                continue
            value = int(value)
            if value == 90:
                coordinate_mode = "absolute"
                coordinate_modes[coordinate_mode] += 1
            elif value == 91:
                coordinate_mode = "incremental"
                coordinate_modes[coordinate_mode] += 1
            elif value == 20:
                units = "inch"
                unit_modes[units] += 1
            elif value == 21:
                units = "mm"
                unit_modes[units] += 1
            elif value in (17, 18, 19):
                plane = {17: "XY", 18: "XZ", 19: "YZ"}[value]
                planes[plane] += 1
            elif value not in _SUPPORTED_G_CODES and value not in (54, 55, 56, 57, 58, 59):
                unknown_g[f"G{value}"].add(line_no)
            if value in (0, 1, 2, 3):
                motion_mode = value
        for value in m_values:
            token = f"M{value}"
            observed_m[token] += 1
            if value not in _KNOWN_M_CODES:
                unknown_m[token].add(line_no)
        for match in _COMPACT_M_T_RE.finditer(code):
            compact_m_t[match.group(0)].add(line_no)
        for letter, value in words:
            if letter == "T" and value.is_integer():
                current_tool = int(value)
                tools[current_tool].add(line_no)
            elif letter == "F":
                current_feed = value
                feeds[f"{value:g}"].add(line_no)
            elif letter == "S":
                current_spindle = value
                spindles[f"{value:g}"].add(line_no)

        axis_words = {letter: value for letter, value in words if letter in "XYZ"}
        if 53 in g_values and axis_words:
            machine_coordinate_lines.add(line_no)
        if not axis_words or motion_mode is None:
            continue
        machine_coordinates = 53 in g_values
        old_position = dict(position)
        old_known = dict(known_position)
        if not machine_coordinates:
            for axis, value in axis_words.items():
                if coordinate_mode == "absolute":
                    position[axis] = value
                    known_position[axis] = True
                elif coordinate_mode == "incremental" and known_position[axis]:
                    position[axis] += value
                else:
                    known_position[axis] = False

        def point_snapshot(pos: dict[str, float], known: dict[str, bool]) -> dict[str, float] | None:
            if known["X"] and known["Y"]:
                return {"x": _rounded(pos["X"]), "y": _rounded(pos["Y"]),
                        "z": _rounded(pos["Z"]) if known["Z"] else None}
            return None

        start = point_snapshot(old_position, old_known)
        end = point_snapshot(position, known_position)
        motion = f"G{motion_mode}"
        move: dict[str, Any] = {
            "source_line": line_no, "motion": motion, "coordinate_mode": coordinate_mode or "unspecified",
            "coordinate_frame": "machine" if machine_coordinates else "program",
            "start": start, "end": end, "raw_axis_words": {a.lower(): _rounded(v) for a, v in axis_words.items()},
            "z": _rounded(position["Z"]) if known_position["Z"] else None,
            "tool": current_tool, "feed": _rounded(current_feed), "spindle": _rounded(current_spindle),
            "raw": raw[:240],
        }
        if motion_mode in (2, 3):
            ij_words = {letter: value for letter, value in words if letter in "IJK"}
            move["arc"] = {"direction": "clockwise" if motion_mode == 2 else "counterclockwise",
                            "plane": plane or "unspecified", "center_offsets": {k.lower(): _rounded(v) for k, v in ij_words.items()}}
            if plane == "XY" and start and end and "I" in ij_words and "J" in ij_words:
                cx, cy = start["x"] + ij_words["I"], start["y"] + ij_words["J"]
                r0 = math.hypot(start["x"] - cx, start["y"] - cy)
                r1 = math.hypot(end["x"] - cx, end["y"] - cy)
                move["arc"].update({"center": {"x": _rounded(cx), "y": _rounded(cy)},
                                    "radius_start": _rounded(r0), "radius_end": _rounded(r1),
                                    "radius_error": _rounded(abs(r0-r1)), "validity": "radius_consistent" if abs(r0-r1) <= 0.05 else "radius_mismatch"})

        if motion_mode == 0 and not machine_coordinates and any(axis in axis_words for axis in "XY"):
            current_block = {
                "block_id": f"block-{len(blocks)+1:04d}", "started_by": "rapid_xy",
                "source_line_start": line_no, "source_line_end": line_no,
                "start": end, "moves": [], "points": [], "depths": set(), "tools": set(),
            }
            if end:
                current_block["points"].append(end)
            blocks.append(current_block)
        elif current_block is None and not machine_coordinates:
            current_block = {
                "block_id": f"block-{len(blocks)+1:04d}", "started_by": "motion_without_rapid_xy",
                "source_line_start": line_no, "source_line_end": line_no,
                "start": start or end, "moves": [], "points": [], "depths": set(), "tools": set(),
            }
            blocks.append(current_block)
        if current_block is not None and not machine_coordinates:
            current_block["source_line_end"] = line_no
            current_block["moves"].append(move)
            if start and motion_mode != 0:
                current_block["points"].append(start)
            if end:
                current_block["points"].append(end)
            if move["motion"] != "G0" and move["z"] is not None:
                current_block["depths"].add(move["z"])
            if current_tool is not None:
                current_block["tools"].add(current_tool)
        if not machine_coordinates:
            all_moves.append(move)

    report_blocks = []
    for block in blocks:
        points = block.pop("points")
        depths = sorted(block.pop("depths"))
        block_tools = sorted(block.pop("tools"))
        xs = [point["x"] for point in points]
        ys = [point["y"] for point in points]
        bounds = None
        if xs and ys:
            bounds = {"x_min": min(xs), "y_min": min(ys), "x_max": max(xs), "y_max": max(ys),
                      "width": _rounded(max(xs)-min(xs)), "height": _rounded(max(ys)-min(ys)),
                      "method": "motion endpoints only; arc extrema may extend beyond these bounds"}
        start, end = block.get("start"), next((m.get("end") for m in reversed(block["moves"]) if m.get("end")), None)
        closed = bool(start and end and math.hypot(end["x"]-start["x"], end["y"]-start["y"]) <= 0.05)
        motion_counts = Counter(move["motion"] for move in block["moves"])
        summarized = {
            "block_id": block["block_id"], "started_by": block["started_by"],
            "source_line_start": block["source_line_start"], "source_line_end": block["source_line_end"],
            "start": start, "end": end, "closed_by_endpoints": closed,
            "motion_counts": dict(sorted(motion_counts.items())), "segment_count": sum(motion_counts.values()),
            "depths": depths, "depth_min": min(depths) if depths else None, "depth_max": max(depths) if depths else None,
            "tools": block_tools, "bounds": bounds, "moves": block["moves"],
        }
        summarized["classification"] = _classify_block(summarized)
        report_blocks.append(summarized)

    path_blocks = [block for block in report_blocks if any(move["motion"] in ("G1", "G2", "G3") for move in block["moves"])]
    legacy_core: dict[str, Any] = {}
    try:
        levels, edge_lo, edge_hi = core.detect_geometry(lines)
        legacy_core["detect_geometry"] = {"ok": True, "hole_levels": len(levels), "edge_lo": _rounded(edge_lo), "edge_hi": _rounded(edge_hi)}
    except core.CncError as exc:
        legacy_core["detect_geometry"] = {"ok": False, "error": str(exc)}
    try:
        zero_result = core.process(text, {"mode": "equal", "delta": 0.0})
        legacy_core["zero_delta_baseline"] = {
            "ok": True, "resizable": bool(zero_result.get("resizable")),
            "length_before": zero_result.get("length_before"), "length_after": zero_result.get("length_after"),
            "gap_count": len(zero_result.get("gaps", [])),
            "hole_count": len(zero_result.get("original", {}).get("holes", [])),
            "contour_count": len(zero_result.get("original", {}).get("contours", [])),
            "zero_delta_text_identical": zero_result.get("cnc_text") == text,
            "output_written": False,
        }
    except core.CncError as exc:
        legacy_core["zero_delta_baseline"] = {"ok": False, "error": str(exc), "output_written": False}

    detector_candidates = [
        block["block_id"] for block in path_blocks
        if block.get("bounds") and block["bounds"]["width"] > 100 and block["bounds"]["height"] > 100
    ]
    warnings = []
    if units is None:
        warnings.append("واحد برنامه صریحاً با G20/G21 تعیین نشده است؛ ابعاد بر حسب واحد برنامه گزارش شده‌اند و میلی‌متر فرض نشده‌اند.")
    if plane is None and any(move["motion"] in ("G2", "G3") for move in all_moves):
        warnings.append("صفحهٔ قوس با G17/G18/G19 صریحاً تعیین نشده؛ مرکز/شعاع قوس‌ها قطعی گزارش نمی‌شود.")
    if any(code in observed_g for code in ("G54", "G55", "G56", "G57", "G58", "G59")):
        warnings.append("افست مبدأ کاری G54-G59 در گزارش اعمال نشده؛ مختصات برنامه معادل مختصات فیزیکی ماشین نیست.")
    if "G43" in observed_g:
        warnings.append("جبران طول ابزار G43/H فقط به‌عنوان دستور مشاهده‌شده ثبت شده و روی مختصات Z اعمال نشده است.")
    if unknown_m:
        warnings.append("دستور M ناشناخته یا وابسته به کنترلر وجود دارد؛ پیش از خروجی اجرایی باید مستند شود.")
    if compact_m_t:
        warnings.append("قالب فشردهٔ M/T ثبت شد؛ بدون پروفایل کنترلر، معنای دقیق آن فرض نشده است.")
    if len(detector_candidates) > 1:
        warnings.append("چند بلوک بزرگ وجود دارد؛ detect_geometry فعلی آخرین بلوک واجد شرط را کانتور فرض می‌کند و ممکن است مسیر سطحی را انتخاب کند.")
    if machine_coordinate_lines:
        warnings.append("حرکت‌های G53 در مختصات ماشین از هندسهٔ برنامه جدا نگه داشته شده‌اند.")

    return {
        "schema_version": "1.0",
        "report_type": "read_only_cnc_diagnostics",
        "input": {"filename": filename, "sha256": digest, "utf8_bytes": len(raw_bytes), "line_count": len(lines), "content_modified": False},
        "safety": {"controller_identified": False, "controller_status": "unknown",
                   "machine_output_written": False, "nonzero_resize_executed": False,
                   "safe_for_execution": False,
                   "statement": "گزارش هندسی/تشخیصی است؛ تشخیص ویژگی معادل تعیین قطعی عملیات فیزیکی نیست."},
        "modal_state": {"coordinate_modes_seen": dict(coordinate_modes), "units_seen": dict(unit_modes),
                        "units_at_end": units or "unspecified", "planes_seen": dict(planes),
                        "plane_at_end": plane or "unspecified"},
        "program_summary": {"motion_line_count": len(all_moves), "block_count": len(report_blocks),
                            "blocks_with_cut_motion": len(path_blocks),
                            "motion_counts": dict(sorted(Counter(move["motion"] for move in all_moves).items())),
                            "tools": [{"number": number, "source_lines": sorted(source_lines)} for number, source_lines in sorted(tools.items())],
                            "feed_values": [{"value": value, "source_lines": sorted(source_lines)} for value, source_lines in sorted(feeds.items())],
                            "spindle_values": [{"value": value, "source_lines": sorted(source_lines)} for value, source_lines in sorted(spindles.items())],
                            "depth_values": sorted({move["z"] for move in all_moves if move["motion"] != "G0" and move["z"] is not None}),
                            "machine_coordinate_lines": sorted(machine_coordinate_lines)},
        "commands": {"observed_g_codes": dict(sorted(observed_g.items())), "observed_m_codes": dict(sorted(observed_m.items())),
                      "unknown_m_codes": [{"token": token, "source_lines": sorted(source_lines)} for token, source_lines in sorted(unknown_m.items())],
                      "compact_m_t_tokens": [{"token": token, "source_lines": sorted(source_lines)} for token, source_lines in sorted(compact_m_t.items())],
                      "unknown_g_codes": [{"token": token, "source_lines": sorted(source_lines)} for token, source_lines in sorted(unknown_g.items())]},
        "legacy_core_baseline": legacy_core,
        "legacy_detector_candidates": {"large_blocks": detector_candidates,
                                       "last_large_block_would_be_selected": detector_candidates[-1] if detector_candidates else None,
                                       "selection_is_heuristic": True},
        "blocks": report_blocks,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a read-only JSON diagnostic report for a CNC G-code file.")
    parser.add_argument("input", help="Path to source .cnc/.nc/.tap file")
    parser.add_argument("--output", help="Write JSON report here; default is stdout")
    parser.add_argument("--encoding", default="utf-8-sig", help="Input text encoding (default: utf-8-sig)")
    args = parser.parse_args(argv)
    source = Path(args.input)
    raw = source.read_bytes()
    try:
        text = raw.decode(args.encoding)
    except UnicodeDecodeError as exc:
        parser.error(f"cannot decode input with {args.encoding}: {exc}")
    report = analyze_text(text, filename=str(source), source_sha256=hashlib.sha256(raw).hexdigest())
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
