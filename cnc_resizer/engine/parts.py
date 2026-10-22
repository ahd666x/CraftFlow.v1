"""Part-level detection on the engine IR (Django-free, stdlib only).

The engine's block/feature layer (``geometry.py`` + ``recognition.py``) sees
one *feature* per G0-split block; a real workpiece ("قطعه") on the sheet is
something else: one outer contour that is often cut at several depths, plus
every hole, pocket, groove and engraving that lives inside it. This module
reconstructs those parts from :func:`cnc_resizer.engine.parser.parse_program`
output and is the single shared detection layer (bug #11 of the archive
review): the parts API, the part-edit lowering and every future consumer
read the same result instead of re-deriving their own.

Detection pipeline (fail-soft — a weird file yields warnings and operations,
never an exception, so no file is ever "display only" again):

1. per block, walk the cut moves and split them into *sub-loops* — not just
   the first closed loop (core's ``_first_loop`` bug) — including the
   single-move full circle case;
2. merge loops that share the same XY geometry within ``merge_tol`` (0.6mm)
   across depth passes into one multi-pass path (Z-14 + Z-16 of one contour
   is ONE part path, and the depth list travels with it);
3. outer-contour candidates: closed paths whose deepest pass reaches
   ``depth_fraction`` (0.8) of the sheet thickness (deepest negative Z in the
   file, or the operator's ``sheet_thickness`` parameter) OR that nothing
   else fully contains; minimum part size ``min_part_size`` (15mm) replaces
   the old ``w>100 and h>100`` threshold;
4. inclusion tree by polygon point-in-loop tests (not bbox): holes, pockets
   and shallow loops become *children* of the smallest containing part;
5. open paths (single saw lines, rasters, grooves) are assigned to the
   nearest part by bbox distance or kept as independent operations with a
   ``kind`` label;
6. partially-overlapping contour candidates (bbox overlap, neither polygon
   contains the other) are both reported but their editing is *locked* with
   an explicit warning — dangerous overlap is never silently edited;
7. shape classification (rect / rect_rounded / tapered / circle / freeform)
   with explicit per-axis capabilities: translation always, band stretch
   only where a straight axial band exists (arcs and diagonals outside the
   band), rotation/mirror for closed contours;
8. filename cross-check (``1970x395`` + ``2set``) as a warning, never an
   error; part finished size = tool-centre bbox +/- tool diameter.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .ir import Program

DEFAULT_MERGE_TOL = 0.6          # mm — same XY path across depths
DEFAULT_MIN_PART_SIZE = 15.0     # mm — smallest outer-contour candidate
DEFAULT_DEPTH_FRACTION = 0.8     # deepest pass must reach 80% of thickness
DEFAULT_MIN_BAND = 10.0          # mm — shortest stretchable straight band
ARC_SAMPLE_STEP_DEG = 15.0
CONNECT_TOL = 0.05               # same tolerance as geometry.py closed test
_STRETCH_TOL = 1e-6


class PartDetectionError(ValueError):
    """Raised only for malformed requests, never for odd geometry."""


# ----------------------------------------------------------------- loops
@dataclass
class Loop:
    block_id: str
    source_lines: tuple[int, int]
    moves: list[Any]
    points: list[tuple[float, float]]       # sampled XY polyline (tool centre)
    bbox: dict[str, float]
    closed: bool
    area: float                              # signed (shoelace), CCW positive
    perimeter: float
    depths: list[float]                      # cut Z values along the loop
    final_depth: float | None
    tool: int | None
    arc_count: int
    start_point: tuple[float, float]


@dataclass
class PartPath:
    """A merged multi-pass path (contour candidate, hole, groove ...)."""
    canonical: Loop
    loops: list[Loop] = field(default_factory=list)

    @property
    def depths(self) -> list[float]:
        values: list[float] = []
        for loop in self.loops:
            values.extend(loop.depths)
        return sorted(set(round(value, 4) for value in values))


def _arc_samples(move: Any) -> list[tuple[float, float]]:
    """Sample one arc move (endpoints + interior angles), XY projected."""
    arc = move.arc
    start, end = move.start, move.end
    pts: list[tuple[float, float]] = []
    if start is not None and start.x is not None and start.y is not None:
        pts.append((start.x, start.y))
    if (arc is None or arc.center is None or arc.radius is None
            or arc.sweep_radians is None or not arc.valid
            or start is None or end is None
            or start.x is None or start.y is None or end.x is None or end.y is None):
        if end is not None and end.x is not None and end.y is not None:
            pts.append((end.x, end.y))
        return pts
    cx, cy = arc.center["x"], arc.center["y"]
    r = arc.radius
    a0 = math.atan2(start.y - cy, start.x - cx)
    sweep = arc.sweep_radians
    n = max(2, int(abs(math.degrees(sweep)) / ARC_SAMPLE_STEP_DEG))
    for k in range(1, n):
        angle = a0 + sweep * k / n
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    pts.append((end.x, end.y))
    return pts


def _linear_samples(move: Any) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    if move.start is not None and move.start.x is not None and move.start.y is not None:
        pts.append((move.start.x, move.start.y))
    if move.end is not None and move.end.x is not None and move.end.y is not None:
        pts.append((move.end.x, move.end.y))
    return pts


def _bbox_of(points: list[tuple[float, float]]) -> dict[str, float] | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {"x_min": min(xs), "y_min": min(ys), "x_max": max(xs), "y_max": max(ys),
            "width": max(xs) - min(xs), "height": max(ys) - min(ys)}


def _shoelace(points: list[tuple[float, float]]) -> float:
    area = 0.0
    count = len(points)
    for index in range(count):
        x0, y0 = points[index]
        x1, y1 = points[(index + 1) % count]
        area += x0 * y1 - x1 * y0
    return area / 2.0


def _extract_loops(program: Program) -> list[Loop]:
    """Split every block's cut moves into sub-loops (all of them, not just
    the first) — core's ``_first_loop`` limitation does not exist here."""
    loops: list[Loop] = []
    for block in program.blocks:
        cut_moves = [move for move in block.motions
                     if move.motion in ("linear", "arc_cw", "arc_ccw")
                     and not move.machine_coordinates]
        current: list[Any] = []
        current_start: tuple[float, float] | None = None
        depths: list[float] = []
        tool: int | None = None

        def flush(cut: list[Any], begin: tuple[float, float] | None,
                 z_values: list[float], loop_tool: int | None) -> None:
            if not cut or begin is None:
                return
            points: list[tuple[float, float]] = []
            arc_count = 0
            perimeter = 0.0
            for move in cut:
                if move.motion in ("arc_cw", "arc_ccw"):
                    arc_count += 1
                    pts = _arc_samples(move)
                else:
                    pts = _linear_samples(move)
                for index in range(len(pts) - 1):
                    perimeter += math.hypot(pts[index + 1][0] - pts[index][0],
                                            pts[index + 1][1] - pts[index][1])
                points.extend(pts)
            # drop consecutive duplicates (end of move N == start of N+1)
            deduped: list[tuple[float, float]] = []
            for point in points:
                if not deduped or math.hypot(point[0] - deduped[-1][0], point[1] - deduped[-1][1]) > _STRETCH_TOL:
                    deduped.append(point)
            last = cut[-1].end
            closed = bool(last is not None and last.x is not None and last.y is not None
                          and math.hypot(last.x - begin[0], last.y - begin[1]) <= CONNECT_TOL)
            bbox = _bbox_of(deduped)
            if bbox is None:
                return
            loops.append(Loop(
                block_id=block.id,
                source_lines=(cut[0].source_line, cut[-1].source_line),
                moves=list(cut),
                points=deduped,
                bbox=bbox,
                closed=closed,
                area=_shoelace(deduped),
                perimeter=perimeter,
                depths=sorted(set(round(value, 4) for value in z_values if value is not None)),
                final_depth=next((value for value in
                                  (move.end.z for move in reversed(cut)
                                   if move.end is not None and move.end.z is not None)), None),
                tool=loop_tool,
                arc_count=arc_count,
                start_point=begin,
            ))

        previous_xy: tuple[float, float] | None = None
        for move in cut_moves:
            if move.tool is not None:
                tool = move.tool
            end = move.end
            if end is None or end.x is None or end.y is None:
                continue
            if move.end.z is not None:
                depths.append(move.end.z)
            start = move.start
            start_xy = (start.x, start.y) if (start is not None and start.x is not None
                                              and start.y is not None) else previous_xy
            if start_xy is None:
                previous_xy = (end.x, end.y)
                continue
            displaced = math.hypot(end.x - start_xy[0], end.y - start_xy[1]) > _STRETCH_TOL
            if not displaced:
                continue  # Z-only plunge/retract carries no loop geometry
            if (current and current_start is not None and previous_xy is not None
                    and math.hypot(start_xy[0] - previous_xy[0],
                                   start_xy[1] - previous_xy[1]) > CONNECT_TOL):
                # the path jumps somewhere else: close what we have first
                flush(current, current_start, depths, tool)
                current, depths, current_start = [], [], start_xy
            if current_start is None:
                current_start = start_xy
            current.append(move)
            previous_xy = (end.x, end.y)
            if (current_start is not None
                    and math.hypot(end.x - current_start[0], end.y - current_start[1]) <= CONNECT_TOL
                    and (len(current) >= 2 or move.motion in ("arc_cw", "arc_ccw"))):
                flush(current, current_start, depths, tool)
                current, depths, current_start = [], [], None
                previous_xy = (end.x, end.y)
        flush(current, current_start, depths, tool)
    return loops


# ------------------------------------------------------- geometry helpers
def _point_in_loop(point: tuple[float, float], loop: Loop, tol: float = 0.0) -> bool:
    """Ray-cast point-in-polygon on the loop's sampled polyline."""
    x, y = point
    inside = False
    pts = loop.points
    count = len(pts)
    for index in range(count):
        x0, y0 = pts[index]
        x1, y1 = pts[(index + 1) % count]
        if (y0 > y) != (y1 > y):
            x_cross = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < x_cross + tol:
                inside = not inside
    return inside


def _contains(outer: Loop, inner: Loop, tol: float) -> bool:
    if (outer.bbox["x_min"] - tol > inner.bbox["x_min"]
            or outer.bbox["x_max"] + tol < inner.bbox["x_max"]
            or outer.bbox["y_min"] - tol > inner.bbox["y_min"]
            or outer.bbox["y_max"] + tol < inner.bbox["y_max"]):
        return False
    if not outer.closed:
        return False
    hits = sum(1 for point in inner.points if _point_in_loop(point, outer, tol))
    return hits >= max(1, int(len(inner.points) * 0.99))


def _mostly_contains(outer: Loop, inner: Loop, tol: float, fraction: float = 0.9) -> bool:
    """Softer inclusion for candidacy: pockets/rabbets that mostly live inside
    a contour but poke out slightly (t.7 side pockets, k.1 score strips) are
    children of that contour, not standalone parts."""
    if (outer.bbox["x_min"] - tol > inner.bbox["x_min"]
            or outer.bbox["x_max"] + tol < inner.bbox["x_max"]
            or outer.bbox["y_min"] - tol > inner.bbox["y_min"]
            or outer.bbox["y_max"] + tol < inner.bbox["y_max"]):
        return False
    if not outer.closed:
        return False
    hits = sum(1 for point in inner.points if _point_in_loop(point, outer, tol))
    return hits >= max(1, int(len(inner.points) * fraction))


def _segments_cross(a: Loop, b: Loop) -> bool:
    """True when any edge segment of ``a`` properly crosses an edge segment of
    ``b`` (orientation test, shared endpoints excluded). Sparse point sampling
    misses crossings between samples; this catches them."""
    def edges(loop: Loop) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        return list(zip(loop.points, loop.points[1:] + loop.points[:1]))

    def orient(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    for (p0, p1) in edges(a):
        for (q0, q1) in edges(b):
            d1 = orient(q0, q1, p0)
            d2 = orient(q0, q1, p1)
            d3 = orient(p0, p1, q0)
            d4 = orient(p0, p1, q1)
            eps = 1e-9
            if ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and \
               ((d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)):
                return True
    return False


def _bbox_close(a: dict[str, float], b: dict[str, float], tol: float) -> bool:
    return all(abs(a[key] - b[key]) <= tol for key in ("x_min", "y_min", "x_max", "y_max"))


def _sample_distance(a: list[tuple[float, float]], b: list[tuple[float, float]],
                     limit: float) -> bool:
    """Bidirectional nearest-point distance between two point clouds ≤ limit.

    Early-exits as soon as one point of ``a`` is farther than ``limit`` from
    every checked point of ``b`` — cheap O(n*m) with a tight inner loop.
    """
    for source, target in ((a, b), (b, a)):
        for px, py in source:
            best = math.inf
            for qx, qy in target:
                distance = math.hypot(px - qx, py - qy)
                if distance < best:
                    best = distance
                    if best <= limit:
                        break
            if best > limit:
                return False
    return True


def _same_geometry(a: Loop, b: Loop, tol: float) -> bool:
    return (_bbox_close(a.bbox, b.bbox, tol)
            and abs(abs(a.area) - abs(b.area)) <= max(tol * 10.0, abs(a.area) * 0.02)
            and abs(a.perimeter - b.perimeter) <= max(tol * 4.0, a.perimeter * 0.02)
            and _sample_distance(a.points, b.points, tol))


def _merge_paths(loops: list[Loop], tol: float) -> list[PartPath]:
    paths: list[PartPath] = []
    for loop in loops:
        for path in paths:
            if _same_geometry(path.canonical, loop, tol):
                path.loops.append(loop)
                break
        else:
            paths.append(PartPath(canonical=loop, loops=[loop]))
    return paths


# --------------------------------------------------------- classification
def _is_circle(loop: Loop) -> bool:
    if loop.arc_count == 0 or loop.arc_count < len(loop.moves):
        return False
    centers_r = []
    for move in loop.moves:
        arc = move.arc
        if (arc is None or arc.center is None or arc.radius is None
                or move.start is None or move.start.x is None or move.start.y is None):
            return False
        centers_r.append((arc.center["x"], arc.center["y"], arc.radius))
    cx = sum(item[0] for item in centers_r) / len(centers_r)
    cy = sum(item[1] for item in centers_r) / len(centers_r)
    if any(math.hypot(item[0] - cx, item[1] - cy) > 0.1 for item in centers_r):
        return False
    radius = sum(item[2] for item in centers_r) / len(centers_r)
    if any(abs(item[2] - radius) > 0.1 for item in centers_r):
        return False
    total_sweep = sum(abs(move.arc.sweep_radians or 0.0) for move in loop.moves)
    return abs(total_sweep - 2 * math.pi) <= 0.2


def _edge_axis_angle(move: Any) -> float | None:
    if move.motion != "linear" or move.start is None or move.end is None:
        return None
    if move.start.x is None or move.start.y is None or move.end.x is None or move.end.y is None:
        return None
    dx, dy = move.end.x - move.start.x, move.end.y - move.start.y
    if abs(dx) < _STRETCH_TOL and abs(dy) < _STRETCH_TOL:
        return None
    return math.degrees(math.atan2(dy, dx)) % 180.0


def classify_shape(loop: Loop) -> str:
    if _is_circle(loop):
        return "circle"
    angles = [angle for angle in (_edge_axis_angle(move) for move in loop.moves)
              if angle is not None]
    # axis-aligned = every edge within 0.5° of a multiple of 90° (0/90/180)
    axis_aligned = all(min(angle % 90.0, 90.0 - angle % 90.0) <= 0.5 for angle in angles)
    arcs = [move for move in loop.moves if move.motion in ("arc_cw", "arc_ccw")]
    if not arcs and axis_aligned and loop.closed:
        return "rect"
    if axis_aligned and loop.closed:
        min_side = min(loop.bbox["width"], loop.bbox["height"])
        radius_max = max((move.arc.radius or 0.0) for move in arcs) if arcs else 0.0
        sweep_ok = all(abs(move.arc.sweep_radians or 0.0) <= math.pi / 2 + 0.05 for move in arcs)
        if radius_max <= max(0.3 * min_side, 6.0) and sweep_ok:
            return "rect_rounded"
        return "freeform"
    if loop.closed and angles:
        return "tapered"
    return "freeform"


def _band_intervals(loop: Loop, axis: str, merge_tol: float) -> list[tuple[float, float]]:
    """Intervals on ``axis`` where the contour is free of arcs and diagonal
    moves — the only zones a segment-level stretch may bridge."""
    forbidden: list[tuple[float, float]] = []
    for move in loop.moves:
        if move.start is None or move.end is None:
            continue
        if move.start.x is None or move.start.y is None or move.end.x is None or move.end.y is None:
            continue
        values = [getattr(move.start, axis), getattr(move.end, axis)]
        if move.arc is not None and move.arc.center and axis in move.arc.center:
            values.append(move.arc.center[axis])
        low, high = min(values), max(values)
        if move.motion in ("arc_cw", "arc_ccw"):
            radius = move.arc.radius if move.arc else None
            if radius:
                center_axis = move.arc.center.get(axis)
                if center_axis is not None:
                    low, high = center_axis - radius, center_axis + radius
            forbidden.append((low, high))
        else:
            cross = "x" if axis == "y" else "y"
            if abs(getattr(move.end, cross) - getattr(move.start, cross)) > _STRETCH_TOL:
                forbidden.append((low, high))
    forbidden.sort()
    merged: list[tuple[float, float]] = []
    for low, high in forbidden:
        if merged and low <= merged[-1][1] + merge_tol:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    bands: list[tuple[float, float]] = []
    cursor = loop.bbox["y_min" if axis == "y" else "x_min"]
    limit = loop.bbox["y_max" if axis == "y" else "x_max"]
    for low, high in merged:
        if low - cursor >= _STRETCH_TOL:
            bands.append((cursor, low))
        cursor = max(cursor, high)
    if limit - cursor >= _STRETCH_TOL:
        bands.append((cursor, limit))
    return bands


def _largest_band(loop: Loop, axis: str, merge_tol: float) -> tuple[float, float] | None:
    bands = _band_intervals(loop, axis, merge_tol)
    if not bands:
        return None
    return max(bands, key=lambda item: item[1] - item[0])


# ------------------------------------------------------------ detection
def _tool_dia_for(tool: int | None, tool_map: dict[int, float], fallback: float) -> float:
    if tool is not None and tool in tool_map:
        return float(tool_map[tool])
    return float(fallback)


def _filename_check(filename: str, sheet: dict[str, float] | None,
                    part_count: int) -> list[str]:
    warnings: list[str] = []
    if not filename:
        return warnings
    # integer-only WxH, last match wins: "t.1.1970x395.2set" -> (1970, 395);
    # the "t.1." family prefix cannot leak in because the W capture stops at
    # the first non-digit (the dot) before the x
    size_matches = re.findall(r"(\d+)[xX](\d+)", filename)
    if size_matches and sheet:
        want_w, want_h = size_matches[-1]
        expected = sorted((float(want_w), float(want_h)))
        actual = sorted((sheet["width"], sheet["height"]))
        for want, got in zip(expected, actual):
            if want > 0 and abs(want - got) / want > 0.02:
                warnings.append(
                    f"ابعاد صفحه ({got:.0f}mm) با نام فایل ({want:.0f}mm) نمی‌خواند؛ "
                    "نام فایل فقط برای هشدار بررسی می‌شود، نه به‌عنوان حقیقت.")
                break
    sets_match = re.search(r"(\d+)\s*sets?", filename, re.IGNORECASE)
    if sets_match:
        declared = int(sets_match.group(1))
        if declared > 0 and part_count not in (0, declared):
            warnings.append(
                f"تعداد قطعهٔ شناسایی‌شده ({part_count}) با تعداد اعلامی نام فایل "
                f"({declared}set) یکی نیست؛ تصمیم نهایی با اپراتور است.")
    return warnings


def part_index(program: Program, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Internal (non-JSON) detection result used by the part-edit lowering."""
    params = params or {}
    merge_tol = float(params.get("merge_tol", DEFAULT_MERGE_TOL))
    min_size = float(params.get("min_part_size", DEFAULT_MIN_PART_SIZE))
    depth_fraction = float(params.get("depth_fraction", DEFAULT_DEPTH_FRACTION))
    tool_map = {int(key): float(value) for key, value in (params.get("tool_map") or {}).items()
                if str(key).lstrip("-").isdigit()}
    fallback_dia = float(params.get("tool_dia", 8.0))

    loops = _extract_loops(program)
    for loop in loops:
        loop.points = [point for point in loop.points
                       if math.isfinite(point[0]) and math.isfinite(point[1])]
    loops = [loop for loop in loops if loop.points]
    warnings: list[str] = []

    all_depths = [value for loop in loops for value in loop.depths if value is not None]
    negatives = [value for value in all_depths if value < 0]
    sheet_thickness = (abs(min(negatives)) if negatives
                       else float(params["sheet_thickness"]) if params.get("sheet_thickness") else None)

    paths = _merge_paths(loops, merge_tol)
    closed_paths = [path for path in paths if path.canonical.closed]
    open_paths = [path for path in paths if not path.canonical.closed]

    def deepest(path: PartPath) -> float | None:
        values = [value for loop in path.loops for value in loop.depths if value is not None]
        return min(values) if values else None

    def size_ok(path: PartPath) -> bool:
        return (path.canonical.bbox["width"] >= min_size
                and path.canonical.bbox["height"] >= min_size)

    # containment matrix over closed paths (polygon test, not bbox):
    # contains[i] = closed paths fully inside path i; mostly[i] = paths that
    # live >=90% inside i (pockets poking out of the contour)
    contains: dict[int, list[int]] = {}
    mostly: dict[int, list[int]] = {}
    for i, outer in enumerate(closed_paths):
        contains[i] = [j for j, inner in enumerate(closed_paths)
                       if j != i and _contains(outer.canonical, inner.canonical, merge_tol)]
        mostly[i] = [j for j, inner in enumerate(closed_paths)
                     if j != i and j not in contains[i]
                     and _mostly_contains(outer.canonical, inner.canonical, merge_tol)]
    contained_by: dict[int, bool] = {i: False for i in range(len(closed_paths))}
    for i in range(len(closed_paths)):
        for j in contains[i]:
            contained_by[j] = True
        for j in mostly[i]:
            contained_by[j] = True

    # paths whose edges CROSS another closed path are machining operations
    # overlapping a contour (t.7 side pockets that cut through the outline),
    # never standalone parts — that case is decided by the candidate-lock
    # intrusion check below (mutual intrusion + _segments_cross), so no
    # separate precomputed crossing table is needed (the old O(n²) pre-pass
    # built a map nobody read).

    candidates: list[int] = []
    for i, path in enumerate(closed_paths):
        if not size_ok(path):
            continue
        depth_ok = False
        if sheet_thickness and sheet_thickness > 0:
            depth = deepest(path)
            depth_ok = depth is not None and abs(depth) >= depth_fraction * sheet_thickness
        else:
            depth_ok = True
        # an outer contour candidate needs: full-depth (or no depth info —
        # a shallow pocket never qualifies) and no other loop containing it
        # (a full-depth window is a child, not a part). Paths that CROSS each
        # other both stay candidates but get locked below — the archive's
        # t.16-style overlays are real parts the operator must see, not
        # silently swallowed operations.
        if depth_ok and not contained_by[i]:
            candidates.append(i)

    # dangerous overlap between candidates: bboxes overlap partially AND the
    # polygons mutually intrude (samples of each inside the other) -> real
    # crossing, editing gets locked. Bbox overlap with disjoint polygons is
    # legitimate interlocking/nesting (wave strips, puzzle rows) and only
    # earns a note; full containment is the normal inclusion tree.
    locked: set[int] = set()
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a = closed_paths[candidates[i]].canonical
            b = closed_paths[candidates[j]].canonical
            overlap_x = min(a.bbox["x_max"], b.bbox["x_max"]) - max(a.bbox["x_min"], b.bbox["x_min"])
            overlap_y = min(a.bbox["y_max"], b.bbox["y_max"]) - max(a.bbox["y_min"], b.bbox["y_min"])
            if overlap_x <= merge_tol or overlap_y <= merge_tol:
                continue
            if _contains(a, b, merge_tol) or _contains(b, a, merge_tol):
                continue  # legitimate nesting, handled by the inclusion tree
            a_in_b = sum(1 for point in a.points if _point_in_loop(point, b, merge_tol))
            b_in_a = sum(1 for point in b.points if _point_in_loop(point, a, merge_tol))
            if (a_in_b > 0 and b_in_a > 0) or _segments_cross(a, b):
                locked.update({candidates[i], candidates[j]})
                warnings.append(
                    "کانتورهای قطعه واقعاً روی هم افتاده‌اند (نفوذ/تقاطع مسیر)؛ هر دو "
                    "شناسایی شدند ولی ویرایششان قفل است — از میزکار ویژگی‌محور استفاده کنید.")
            else:
                warnings.append(
                    "قطعه‌های درهم‌رفته (interlocking؛ bbox هم‌پوشان ولی مسیرهای جدا) "
                    "شناسایی شدند؛ فاصلهٔ برش را بررسی کنید.")

    # children: non-candidate closed paths -> smallest containing candidate
    # (strong containment first, then the >=90% "mostly inside" tier so a
    # pocket that pokes out of the contour still travels with its part)
    children_of: dict[int, list[int]] = {index: [] for index in candidates}
    orphan_closed: list[int] = []
    for i, path in enumerate(closed_paths):
        if i in candidates:
            continue
        parents = sorted({j for j in range(len(closed_paths))
                          if i in contains[j] or i in mostly[j]},
                         key=lambda parent: (
                             closed_paths[parent].canonical.bbox["width"]
                             * closed_paths[parent].canonical.bbox["height"]))
        parent = next((index for index in parents if index in candidates), None)
        if parent is None:
            orphan_closed.append(i)
        else:
            children_of[parent].append(i)

    # build parts
    parts: list[dict[str, Any]] = []
    for order, index in enumerate(sorted(candidates, key=lambda k: (
            closed_paths[k].canonical.bbox["y_min"], closed_paths[k].canonical.bbox["x_min"]))):
        path = closed_paths[index]
        canonical = path.canonical
        loop_tool = canonical.tool
        dia = _tool_dia_for(loop_tool, tool_map, fallback_dia)
        shape = classify_shape(canonical)
        band_y = _largest_band(canonical, "y", merge_tol)
        band_x = _largest_band(canonical, "x", merge_tol)
        is_locked = index in locked
        capabilities = {
            "can_translate": not is_locked,
            "can_stretch_y": (not is_locked and band_y is not None
                              and band_y[1] - band_y[0] >= DEFAULT_MIN_BAND),
            "can_stretch_x": (not is_locked and band_x is not None
                              and band_x[1] - band_x[0] >= DEFAULT_MIN_BAND),
            "can_rotate90": not is_locked,
            "can_mirror": not is_locked,
        }
        bbox = {key: round(value, 4) for key, value in canonical.bbox.items()}
        bbox_finished = {
            "x_min": round(bbox["x_min"] - dia / 2, 4), "x_max": round(bbox["x_max"] + dia / 2, 4),
            "y_min": round(bbox["y_min"] - dia / 2, 4), "y_max": round(bbox["y_max"] + dia / 2, 4),
        }
        bbox_finished["width"] = round(bbox_finished["x_max"] - bbox_finished["x_min"], 4)
        bbox_finished["height"] = round(bbox_finished["y_max"] - bbox_finished["y_min"], 4)
        block_ids = sorted({loop.block_id for loop in path.loops})
        passes = sorted(({"z": loop.final_depth, "block_id": loop.block_id,
                          "source_lines": [loop.source_lines[0], loop.source_lines[1]]}
                         for loop in path.loops), key=lambda item: item["source_lines"][0])
        signature = "|".join([
            f"{bbox['x_min']:.2f}", f"{bbox['y_min']:.2f}", f"{bbox['x_max']:.2f}",
            f"{bbox['y_max']:.2f}", shape,
            ",".join(f"{value:.2f}" for value in path.depths),
            f"{abs(canonical.area):.1f}"])
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:10]
        parts.append({
            "id": f"part-{digest}",
            "_canonical": canonical,
            "index": order + 1,
            "shape_kind": shape,
            "bbox": bbox,
            "bbox_finished": bbox_finished,
            "tool_dia_used": dia,
            "tool": loop_tool,
            "contour_block_ids": block_ids,
            "contour_feature_ids": [f"feature-{block_id}" for block_id in block_ids],
            "passes": passes,
            "depths": path.depths,
            "area_path": round(abs(canonical.area), 2),
            "direction": "ccw" if canonical.area > 0 else "cw",
            "arc_count": canonical.arc_count,
            "capabilities": capabilities,
            "stretch_band_y": [round(band_y[0], 4), round(band_y[1], 4)] if band_y else None,
            "stretch_band_x": [round(band_x[0], 4), round(band_x[1], 4)] if band_x else None,
            "children": [],
            "warnings": ["ویرایش این قطعه به‌دلیل هم‌پوشانی با قطعهٔ دیگر قفل است."] if is_locked else [],
            "locked": is_locked,
            "_index": index,
        })

    # twins: identical geometry signature -> deterministic suffix
    seen_ids: dict[str, int] = {}
    for part in parts:
        first = seen_ids.get(part["id"])
        if first is None:
            seen_ids[part["id"]] = 1
        else:
            seen_ids[part["id"]] = first + 1
            part["id"] = f"{part['id']}#{first + 1}"

    # children + open paths
    operations: list[dict[str, Any]] = []
    part_bodies = [part for part in parts]

    def nearest_part(bbox: dict[str, float]) -> int | None:
        best, best_distance = None, math.inf
        cx = (bbox["x_min"] + bbox["x_max"]) / 2
        cy = (bbox["y_min"] + bbox["y_max"]) / 2
        for number, part in enumerate(part_bodies):
            pb = part["bbox"]
            dx = max(pb["x_min"] - cx, 0.0, cx - pb["x_max"])
            dy = max(pb["y_min"] - cy, 0.0, cy - pb["y_max"])
            distance = math.hypot(dx, dy)
            if distance < best_distance - _STRETCH_TOL:
                best, best_distance = number, distance
        return best

    for parent_index, child_indices in children_of.items():
        parent = next(part for part in parts if part["_index"] == parent_index)
        for child_index in sorted(child_indices, key=lambda k: (
                closed_paths[k].canonical.bbox["y_min"], closed_paths[k].canonical.bbox["x_min"])):
            child = closed_paths[child_index]
            child_dia = _tool_dia_for(child.canonical.tool, tool_map, fallback_dia)
            shallow = bool(sheet_thickness and deepest(child) is not None
                           and abs(deepest(child)) < depth_fraction * sheet_thickness)
            child_shape = classify_shape(child.canonical)
            child_block_ids = sorted({loop.block_id for loop in child.loops})
            parent["children"].append({
                "id": f"child-{parent['index']}-{len(parent['children']) + 1}",
                "kind": ("pocket_or_hole" if not shallow else "engraving_loop"),
                "shape": child_shape,
                "bbox": {key: round(value, 4) for key, value in child.canonical.bbox.items()},
                "tool_dia_used": child_dia,
                "block_ids": child_block_ids,
                "feature_ids": [f"feature-{block_id}" for block_id in child_block_ids],
                "depths": sorted({value for loop in child.loops for value in loop.depths}),
                "closed": True,
            })

    for path in open_paths:
        canonical = path.canonical
        dia = _tool_dia_for(canonical.tool, tool_map, fallback_dia)
        block_ids = sorted({loop.block_id for loop in path.loops})
        long_passes = 0
        directions = []
        straight_lines = 0
        for move in canonical.moves:
            if move.motion != "linear" or move.start is None or move.end is None:
                continue
            dx, dy = move.end.x - move.start.x, move.end.y - move.start.y
            if abs(dx) >= 50 and abs(dy) <= max(15.0, abs(dx) * 0.03):
                long_passes += 1
                directions.append(1 if dx > 0 else -1)
            # a single straight cut along EITHER axis (vertical saw line too)
            if (max(abs(dx), abs(dy)) >= 50 and min(abs(dx), abs(dy)) <= 2.0):
                straight_lines += 1
        alternation = sum(a != b for a, b in zip(directions, directions[1:]))
        ratio = alternation / max(1, len(directions) - 1)
        narrow = min(canonical.bbox["width"], canonical.bbox["height"]) <= 2.0
        if long_passes >= 4 and ratio >= 0.6:
            kind = "raster"
        elif straight_lines <= 3 and narrow and canonical.arc_count == 0 \
                and max(canonical.bbox["width"], canonical.bbox["height"]) >= 50:
            kind = "saw_line"
        else:
            kind = "groove"
        shallow = bool(sheet_thickness and deepest(path) is not None
                       and abs(deepest(path)) < depth_fraction * sheet_thickness)
        if shallow and kind in ("groove", "saw_line"):
            kind = "engraving"
        target = nearest_part(canonical.bbox)
        entry = {
            "id": f"op-{len(operations) + 1:03d}",
            "kind": kind,
            "bbox": {key: round(value, 4) for key, value in canonical.bbox.items()},
            "tool_dia_used": dia,
            "block_ids": block_ids,
            "feature_ids": [f"feature-{block_id}" for block_id in block_ids],
            "passes": sorted(({"z": loop.final_depth, "block_id": loop.block_id,
                               "source_lines": [loop.source_lines[0], loop.source_lines[1]]}
                              for loop in path.loops), key=lambda item: item["source_lines"][0]),
            "shallow": shallow,
            "assigned_part": None,
            "closed": False,
        }
        if target is not None:
            entry["assigned_part"] = part_bodies[target]["id"]
            part_bodies[target].setdefault("_op_children", []).append(entry)
        operations.append(entry)

    for orphan_index in orphan_closed:
        child = closed_paths[orphan_index]
        dia = _tool_dia_for(child.canonical.tool, tool_map, fallback_dia)
        block_ids = sorted({loop.block_id for loop in child.loops})
        operations.append({
            "id": f"op-{len(operations) + 1:03d}",
            "kind": "orphan_loop",
            "bbox": {key: round(value, 4) for key, value in child.canonical.bbox.items()},
            "tool_dia_used": dia,
            "block_ids": block_ids,
            "feature_ids": [f"feature-{block_id}" for block_id in block_ids],
            "passes": sorted(({"z": loop.final_depth, "block_id": loop.block_id,
                               "source_lines": [loop.source_lines[0], loop.source_lines[1]]}
                              for loop in child.loops), key=lambda item: item["source_lines"][0]),
            "shallow": bool(sheet_thickness and deepest(child) is not None
                            and abs(deepest(child)) < depth_fraction * sheet_thickness),
            "assigned_part": None,
            "closed": True,
        })
        warnings.append(
            f"حلقهٔ بستهٔ خارج از همهٔ قطعه‌ها به‌عنوان عملیات مستقل ثبت شد (op-{len(operations):03d}).")

    # clean internal fields, sheet bbox, filename cross-check
    for part in parts:
        part.pop("_index", None)
        ops = part.pop("_op_children", [])
        for entry in ops:
            part["children"].append(entry)
    geometries = [loop.bbox for loop in loops]
    sheet = None
    if geometries:
        sheet = {
            "x_min": min(item["x_min"] for item in geometries),
            "y_min": min(item["y_min"] for item in geometries),
            "x_max": max(item["x_max"] for item in geometries),
            "y_max": max(item["y_max"] for item in geometries),
        }
        sheet["width"] = sheet["x_max"] - sheet["x_min"]
        sheet["height"] = sheet["y_max"] - sheet["y_min"]
        sheet = {key: round(value, 4) for key, value in sheet.items()}
    sheet_finished = None
    if sheet is not None:
        dias = [_tool_dia_for(loop.tool, tool_map, fallback_dia) for loop in loops]
        margin = max(dias) / 2 if dias else fallback_dia / 2
        sheet_finished = {
            "x_min": round(sheet["x_min"] - margin, 4), "y_min": round(sheet["y_min"] - margin, 4),
            "x_max": round(sheet["x_max"] + margin, 4), "y_max": round(sheet["y_max"] + margin, 4),
        }
        sheet_finished["width"] = round(sheet_finished["x_max"] - sheet_finished["x_min"], 4)
        sheet_finished["height"] = round(sheet_finished["y_max"] - sheet_finished["y_min"], 4)

    warnings.extend(_filename_check(program.filename or "", sheet_finished, len(parts)))
    if not parts and not operations:
        warnings.append("هندسهٔ برش قابل‌تشخیصی در فایل پیدا نشد؛ هیچ قطعه یا عملیاتی ثبت نشد.")
    if sheet_thickness is None:
        warnings.append("عمق برش منفی در فایل یافت نشد؛ ضخامت ورق نامشخص و قاعدهٔ عمق غیرفعال است.")

    return {
        "parts": parts,
        "operations": operations,
        "sheet_bbox": sheet,
        "sheet_bbox_finished": sheet_finished,
        "sheet_thickness": round(sheet_thickness, 4) if sheet_thickness is not None else None,
        "warnings": warnings,
        "closed_path_count": len(closed_paths),
        "open_path_count": len(open_paths),
    }


def detect_parts(program: Program, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """JSON-safe part detection (see module docstring for the pipeline)."""
    return _strip_internal(part_index(program, params))


def _strip_internal(value: Any) -> Any:
    """Drop every underscore-prefixed key (internal loop objects etc.) so the
    detection result is plain JSON-safe data."""
    if isinstance(value, dict):
        return {key: _strip_internal(item) for key, item in value.items()
                if not key.startswith("_")}
    if isinstance(value, list):
        return [_strip_internal(item) for item in value]
    return value
