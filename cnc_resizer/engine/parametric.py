"""Constraint-based preview transforms. This module never serializes G-code."""
from __future__ import annotations

import copy
import math
import re
from typing import Any

from .geometry import program_geometry
from .ir import Program, to_dict
from .parser import parse_program
from .recognition import recognize
from .serializer import SerializerBlocked, serialize_candidate
from .validation import validate_equivalence


class TransformError(ValueError):
    pass


# --- geometry-verified collision refinement ---------------------------------
# The bounds-level gate is deliberately conservative: a bbox-level partial
# overlap that an edit CREATES is rejected. But bboxes overlap whenever two
# shapes are near each other (L-shapes interleaved, a part pushed 0.5 mm
# toward a neighbour whose real paths stay far away), so operators on tightly
# nested sheets hit "newly partially overlap" for edits that cannot collide.
# The refinement checks the RUNTIME cutting paths themselves: crossing paths
# stay a hard error, bbox-only overlaps become a non-blocking warning that
# still travels to the map UI (collision_check.flagged, severity="warning").
_GEOM_PAIR_BUDGET = 400_000   # max segment-count product per checked pair
_ARC_SAMPLE_STEP = math.pi / 12   # 15° per chord


def _feature_cut_segments(prog: Program, block_id: str) -> list[tuple[float, float, float, float]]:
    """Flat cutting segments (x1, y1, x2, y2) of one block; arcs are chord-sampled.

    Rapid/plunge moves are excluded — the gate is about cut paths. This mirrors
    the block-bounds convention (cuts only), so bbox-partial pairs are exactly
    the pairs this geometry can clear or condemn.
    """
    segs: list[tuple[float, float, float, float]] = []
    for block in prog.blocks:
        if block.id != block_id:
            continue
        for move in block.motions:
            if move.motion not in ("linear", "arc_cw", "arc_ccw"):
                continue
            if move.start is None or move.end is None:
                continue
            x1, y1, x2, y2 = move.start.x, move.start.y, move.end.x, move.end.y
            if x1 is None or y1 is None or x2 is None or y2 is None:
                continue
            arc = move.arc
            if (move.motion in ("arc_cw", "arc_ccw") and arc is not None and arc.valid
                    and arc.center is not None and arc.center.get("x") is not None
                    and arc.center.get("y") is not None and arc.radius and arc.sweep_radians):
                cx, cy = arc.center["x"], arc.center["y"]
                steps = max(2, min(96, int(math.ceil(abs(arc.sweep_radians) / _ARC_SAMPLE_STEP))))
                a0 = math.atan2(y1 - cy, x1 - cx)
                px, py = x1, y1
                for k in range(1, steps + 1):
                    ang = a0 + arc.sweep_radians * (k / steps)
                    qx, qy = cx + arc.radius * math.cos(ang), cy + arc.radius * math.sin(ang)
                    segs.append((px, py, qx, qy))
                    px, py = qx, qy
            else:
                segs.append((x1, y1, x2, y2))
    return segs


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> int:
    v = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if abs(v) < 1e-12:
        return 0
    return 1 if v > 0 else -1


def _on_seg(ax: float, ay: float, bx: float, by: float, px: float, py: float) -> bool:
    return (min(ax, bx) - 1e-9 <= px <= max(ax, bx) + 1e-9
            and min(ay, by) - 1e-9 <= py <= max(ay, by) + 1e-9)


def _seg_intersect(s1: tuple[float, float, float, float],
                   s2: tuple[float, float, float, float]) -> bool:
    ax1, ay1, ax2, ay2 = s1
    bx1, by1, bx2, by2 = s2
    # bbox rejection first (cheap)
    if (max(ax1, ax2) < min(bx1, bx2) - 1e-12 or max(bx1, bx2) < min(ax1, ax2) - 1e-12
            or max(ay1, ay2) < min(by1, by2) - 1e-12 or max(by1, by2) < min(ay1, ay2) - 1e-12):
        return False
    d1 = _orient(bx1, by1, bx2, by2, ax1, ay1)
    d2 = _orient(bx1, by1, bx2, by2, ax2, ay2)
    d3 = _orient(ax1, ay1, ax2, ay2, bx1, by1)
    d4 = _orient(ax1, ay1, ax2, ay2, bx2, by2)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    if d1 == 0 and _on_seg(bx1, by1, bx2, by2, ax1, ay1):
        return True
    if d2 == 0 and _on_seg(bx1, by1, bx2, by2, ax2, ay2):
        return True
    if d3 == 0 and _on_seg(ax1, ay1, ax2, ay2, bx1, by1):
        return True
    if d4 == 0 and _on_seg(ax1, ay1, ax2, ay2, bx2, by2):
        return True
    return False


def _point_seg_distance(px: float, py: float,
                        x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    span2 = dx * dx + dy * dy
    if span2 <= 1e-18:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / span2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _segments_intersect_any(segs_a, segs_b) -> bool:
    for sa in segs_a:
        for sb in segs_b:
            if _seg_intersect(sa, sb):
                return True
    return False


def _segments_min_distance(segs_a, segs_b) -> float:
    best = float("inf")
    for (ax1, ay1, ax2, ay2) in segs_a:
        a_lo = (min(ax1, ax2), min(ay1, ay2))
        a_hi = (max(ax1, ax2), max(ay1, ay2))
        for (bx1, by1, bx2, by2) in segs_b:
            bx_lo = min(bx1, bx2)
            bx_hi = max(bx1, bx2)
            by_lo = min(by1, by2)
            by_hi = max(by1, by2)
            dx_bb = max(a_lo[0] - bx_hi, bx_lo - a_hi[0], 0.0)
            dy_bb = max(a_lo[1] - by_hi, by_lo - a_hi[1], 0.0)
            if math.hypot(dx_bb, dy_bb) >= best:
                continue
            if _seg_intersect((ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2)):
                return 0.0
            best = min(best,
                       _point_seg_distance(ax1, ay1, bx1, by1, bx2, by2),
                       _point_seg_distance(ax2, ay2, bx1, by1, bx2, by2),
                       _point_seg_distance(bx1, by1, ax1, ay1, ax2, ay2),
                       _point_seg_distance(bx2, by2, ax1, ay1, ax2, ay2))
    return best


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TransformError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise TransformError(f"{label} must be finite")
    return number


def _point_coordinates(point: Any, axis: str) -> float | None:
    return getattr(point, axis)


def _coordinate_map(value: float, start: float, end: float, delta: float) -> float:
    if value <= start:
        return value
    if value >= end:
        return value + delta
    factor = (end - start + delta) / (end - start)
    return start + (value - start) * factor


def _arc_offset_preserving(move: Any, axis: str, mapping) -> bool:
    arc = move.arc
    if not arc or not arc.center or arc.plane != "XY":
        return False
    center_value = arc.center[axis]
    values = [_point_coordinates(move.start, axis), _point_coordinates(move.end, axis), center_value]
    offsets = [mapping(value) - value for value in values]
    return max(offsets) - min(offsets) <= 1e-7


def _feature_axis_values(block: Any, axis: str) -> list[float]:
    return [value for move in block.motions if move.motion in ("linear", "arc_cw", "arc_ccw")
            for point in (move.start, move.end) if point is not None
            for value in (getattr(point, axis),) if value is not None]


def _feature_axis_measure(block: Any, axis: str, basis: str) -> float:
    values = _feature_axis_values(block, axis)
    if not values:
        raise TransformError(f"Feature has unresolved {axis} coordinates")
    if basis == "min_to_min":
        return min(values)
    if basis == "max_to_max":
        return max(values)
    if basis == "center_to_center":
        return (min(values) + max(values)) / 2
    raise TransformError(f"Unsupported dependency basis: {basis}")


def _transform_feature(block: Any, axis: str, mapping) -> None:
    for move in block.motions:
        if move.motion not in ("linear", "arc_cw", "arc_ccw"):
            continue
        for point_name in ("start", "end"):
            point = getattr(move, point_name)
            if point is not None and getattr(point, axis) is not None:
                setattr(point, axis, mapping(getattr(point, axis)))
        if move.arc and move.arc.center and axis in move.arc.center:
            move.arc.center[axis] = mapping(move.arc.center[axis])


def _stretch_closed_feature(block: Any, axis: str, start: float, end: float,
                            delta: float, feature_id: str) -> None:
    """Segment-level stretch of a closed path (fail-closed).

    A closed path cannot be scaled through the reference band without
    distorting its arcs. Instead only axial straight moves (parallel to the
    stretch axis) may bridge the band: everything wholly below ``start``
    stays rigid, everything wholly above ``end`` translates by ``delta``, and
    each axial straight move that connects the two rigid regions absorbs the
    length change at its junctions. Arcs and diagonal moves crossing the band
    would be distorted and are rejected instead of being guessed at.
    """
    tol = 1e-6
    cross = "x" if axis == "y" else "y"
    moves = [move for move in block.motions if move.motion in ("linear", "arc_cw", "arc_ccw")]
    if len(moves) < 3:
        raise TransformError(f"Feature {feature_id} does not contain a stretchable closed path")
    first_start, last_end = moves[0].start, moves[-1].end
    if (first_start is None or last_end is None
            or getattr(first_start, "x") is None or getattr(first_start, "y") is None
            or getattr(last_end, "x") is None or getattr(last_end, "y") is None
            or math.hypot(first_start.x - last_end.x, first_start.y - last_end.y) > 0.05):
        raise TransformError(f"Feature {feature_id} is not a connected closed path; segment-level stretch is not defined")

    def axial_interval(move: Any) -> tuple[float, float]:
        values = [getattr(move.start, axis), getattr(move.end, axis)]
        if move.arc and move.arc.center and axis in move.arc.center:
            values.append(move.arc.center[axis])
        return min(values), max(values)

    groups: list[str] = []
    for move in moves:
        if move.start is None or move.end is None or getattr(move.start, axis) is None or getattr(move.end, axis) is None:
            raise TransformError(f"Feature {feature_id} has unresolved {axis} coordinates")
        low, high = axial_interval(move)
        if high <= start + tol:
            groups.append("below")
        elif low >= end - tol:
            groups.append("above")
        elif move.motion != "linear":
            raise TransformError(
                f"Feature {feature_id}: segment-level closed stretch keeps arcs rigid; "
                "an arc crosses the reference band and would be distorted")
        elif abs(getattr(move.end, cross) - getattr(move.start, cross)) > tol:
            raise TransformError(
                f"Feature {feature_id}: segment-level closed stretch lengthens only axial straight moves; "
                "a diagonal move crosses the reference band and would be distorted")
        else:
            groups.append("band")

    count = len(moves)
    offsets: list[float | None] = [None] * count

    def pin(index: int, value: float) -> None:
        if offsets[index] is not None and abs(offsets[index] - value) > 1e-9:
            raise TransformError(
                f"Feature {feature_id}: no consistent segment-level stretch exists for this closed path")
        offsets[index] = value

    for index, group in enumerate(groups):
        if group == "below":
            pin(index, 0.0)
            pin((index + 1) % count, 0.0)
        elif group == "above":
            pin(index, delta)
            pin((index + 1) % count, delta)
        else:  # axial band move: junctions follow the neighbouring rigid regions
            pin(index, delta if groups[index - 1] == "above" else 0.0)
            pin((index + 1) % count, delta if groups[(index + 1) % count] == "above" else 0.0)

    if abs(delta) > 1e-9 and not (any(abs(value) > 1e-9 for value in offsets)
                                  and any(abs(value) <= 1e-9 for value in offsets)):
        raise TransformError(
            f"Feature {feature_id}: the reference band does not separate two rigid regions "
            "of the closed path; nothing would stretch consistently")
    if offsets[0] is not None and abs(offsets[0]) > 1e-9:
        # The loop's entry point would move. The positioning rapid before the
        # loop is the only thing that delivers the tool there; patching it is
        # not part of the closed-stretch contract, so a moved entry point
        # would cut a diagonal from the stale rapid position. Fail closed.
        raise TransformError(
            f"Feature {feature_id}: this reference band would move the closed path's entry point; "
            "the positioning rapid before the loop cannot be patched by closed stretch — "
            "choose a band that keeps the loop start rigid")

    for index, move in enumerate(moves):
        start_offset = offsets[index]
        end_offset = offsets[(index + 1) % count]
        if move.arc and move.arc.center and axis in move.arc.center and abs(start_offset - end_offset) > 1e-9:
            raise TransformError(f"Feature {feature_id}: arc {move.id} would be distorted by the band stretch")
        if move.start is not None and getattr(move.start, axis) is not None:
            setattr(move.start, axis, getattr(move.start, axis) + start_offset)
        if move.end is not None and getattr(move.end, axis) is not None:
            setattr(move.end, axis, getattr(move.end, axis) + end_offset)
        if move.arc and move.arc.center and axis in move.arc.center:
            move.arc.center[axis] = move.arc.center[axis] + start_offset


def preview_resize(program: Program, request: dict[str, Any]) -> dict[str, Any]:
    """Return a transformed IR preview and validation, never G-code text.

    ``features`` maps every recognition feature ID to a rule: ``fixed``,
    ``middle_scale``, ``dependent_end`` or a ``dependent_feature`` relation.
    An operator must choose a rule for each feature.
    """
    if not isinstance(request, dict):
        raise TransformError("Preview request must be an object")
    axis = str(request.get("axis", "y")).lower()
    if axis not in ("x", "y"):
        raise TransformError("Only X or Y can be previewed in phase one")
    original = recognize(program)
    overall = original["bounds"]
    if not overall:
        raise TransformError("No resolved geometry bounds are available")
    low_key, high_key = f"{axis}_min", f"{axis}_max"
    current_dimension = overall[high_key] - overall[low_key]
    target_dimension = _finite_number(request.get("target_dimension"), "target_dimension")
    start = _finite_number(request.get("reference_start"), "reference_start")
    end = _finite_number(request.get("reference_end"), "reference_end")
    if target_dimension <= 0:
        raise TransformError("target_dimension must be greater than zero")
    if end <= start:
        raise TransformError("reference_end must be greater than reference_start")
    if start < overall[low_key] - 1e-6 or end > overall[high_key] + 1e-6:
        raise TransformError("Reference points must lie within the source geometry bounds")
    span = end - start
    delta = target_dimension - current_dimension
    if span + delta <= 1e-6:
        raise TransformError("Resize would collapse or invert the reference span")

    modes = request.get("features") or {}
    if not isinstance(modes, dict):
        raise TransformError("features must be an object keyed by feature ID")
    known_ids = {feature["id"] for feature in original["features"]}
    missing_ids = known_ids - set(modes)
    if missing_ids:
        raise TransformError("An operator-selected rule is required for every feature: " + ", ".join(sorted(missing_ids)))
    extra_ids = set(modes) - known_ids
    if extra_ids:
        raise TransformError("Unknown feature IDs: " + ", ".join(sorted(extra_ids)))
    allowed_modes = {"fixed", "stretch", "middle_scale", "dependent_end", "dependent_feature"}
    normalized_rules: dict[str, dict[str, Any]] = {}
    invalid = {}
    for feature_id, rule in modes.items():
        if isinstance(rule, str):
            rule = {"mode": rule}
        if not isinstance(rule, dict) or rule.get("mode") not in allowed_modes:
            invalid[feature_id] = rule
            continue
        if rule["mode"] == "dependent_feature":
            anchor = rule.get("anchor_feature_id")
            if not isinstance(anchor, str) or anchor not in known_ids or anchor == feature_id:
                invalid[feature_id] = "dependent_feature requires another known anchor_feature_id"
                continue
            basis = rule.get("basis", "min_to_min")
            if basis not in ("min_to_min", "max_to_max", "center_to_center"):
                invalid[feature_id] = "unsupported dependent-feature basis"
                continue
            rule = {**rule, "basis": basis}
            if "offset" in rule:
                try:
                    rule = {**rule, "offset": _finite_number(rule["offset"], f"{feature_id}.offset")}
                except TransformError:
                    invalid[feature_id] = "offset must be finite"
                    continue
        normalized_rules[feature_id] = rule
    if invalid:
        raise TransformError("Unsupported feature modes: " + ", ".join(sorted(invalid)))
    feature_by_block = {feature["block_id"]: feature for feature in original["features"]}
    block_by_id = {block.id: block for block in program.blocks}
    mapping = lambda value: _coordinate_map(value, start, end, delta)
    transformed = copy.deepcopy(program)
    transformed_blocks = {block.id: block for block in transformed.blocks}
    changed_features = []
    constraints = []
    applied: set[str] = set()
    resolving: set[str] = set()
    feature_by_id = {feature["id"]: feature for feature in original["features"]}

    def apply_feature(feature_id: str) -> None:
        if feature_id in applied:
            return
        if feature_id in resolving:
            raise TransformError("Dependent-feature rules contain a cycle")
        resolving.add(feature_id)
        feature = feature_by_id[feature_id]
        block = transformed_blocks[feature["block_id"]]
        rule = normalized_rules[feature_id]
        mode = rule["mode"]
        cut_moves = [move for move in block.motions if move.motion in ("linear", "arc_cw", "arc_ccw")]
        if mode == "fixed":
            constraints.append({"feature_id": feature_id, "rule": "fixed", "result": "preserve all toolpath coordinates"})
        elif mode == "dependent_feature":
            anchor_id = rule["anchor_feature_id"]
            apply_feature(anchor_id)
            anchor_block = transformed_blocks[feature_by_id[anchor_id]["block_id"]]
            basis = rule["basis"]
            original_offset = (
                _feature_axis_measure(block_by_id[feature["block_id"]], axis, basis)
                - _feature_axis_measure(block_by_id[feature_by_id[anchor_id]["block_id"]], axis, basis)
            )
            offset = rule.get("offset", original_offset)
            target_measure = _feature_axis_measure(anchor_block, axis, basis) + offset
            translation = target_measure - _feature_axis_measure(block, axis, basis)
            _transform_feature(block, axis, lambda value: value + translation)
            constraints.append({"feature_id": feature_id, "rule": "dependent_feature", "anchor_feature_id": anchor_id,
                                "basis": basis, "offset": offset, "translation": translation,
                                "offset_preserved": True})
            if feature_id not in changed_features and abs(translation) > 1e-9:
                changed_features.append(feature_id)
        elif mode == "dependent_end":
            coordinates = _feature_axis_values(block, axis)
            if not coordinates or min(coordinates) < end - 1e-6:
                raise TransformError(f"Feature {feature_id} is not wholly beyond the end reference; rigid dependency cannot be guaranteed")
            _transform_feature(block, axis, lambda value: value + delta)
            constraints.append({"feature_id": feature_id, "rule": "dependent_end", "translation": delta})
            if abs(delta) > 1e-9:
                changed_features.append(feature_id)
        else:
            if not cut_moves:
                raise TransformError(f"Feature {feature_id} has no cutting motion")
            if mode == "middle_scale" and feature["kind"] in ("small_loop_candidate", "large_closed_path_candidate"):
                raise TransformError(f"Feature {feature_id} is closed; middle scaling would distort it")
            if mode == "stretch" and feature["kind"] == "small_loop_candidate":
                raise TransformError(f"Feature {feature_id} is a small closed loop; stretching it is prohibited")
            if mode == "stretch" and feature["kind"] == "large_closed_path_candidate":
                # Package-B segment-level stretch: axial straight moves bridge the
                # band, arcs/non-axial moves stay rigid or the transform fails.
                _stretch_closed_feature(block, axis, start, end, delta, feature_id)
                constraints.append({"feature_id": feature_id, "rule": "stretch",
                                    "strategy": "segment_level_closed", "reference_start": start,
                                    "reference_end": end, "target_dimension": target_dimension})
                changed_features.append(feature_id)
                resolving.remove(feature_id)
                applied.add(feature_id)
                return
            coordinates = [getattr(point, axis) for move in cut_moves for point in (move.start, move.end)
                           if point is not None and getattr(point, axis) is not None]
            if len(coordinates) < 2:
                raise TransformError(f"Feature {feature_id} has unresolved coordinates")
            feature_map = mapping
            if any(move.arc for move in cut_moves):
                if any(move.arc and not _arc_offset_preserving(move, axis, feature_map) for move in cut_moves):
                    raise TransformError(f"Feature {feature_id} contains an arc that would be distorted by this transform")
            _transform_feature(block, axis, feature_map)
            constraints.append({"feature_id": feature_id, "rule": mode, "reference_start": start,
                                "reference_end": end, "target_dimension": target_dimension})
            changed_features.append(feature_id)
        resolving.remove(feature_id)
        applied.add(feature_id)

    for block_id, feature in feature_by_block.items():
        apply_feature(feature["id"])

    updated_geometry = program_geometry(transformed)
    updated_bounds = updated_geometry["bounds"]
    resulting_dimension = updated_bounds[high_key] - updated_bounds[low_key] if updated_bounds else None
    validation_errors = []
    validation_warnings = []
    effective_modes = {feature["id"]: normalized_rules[feature["id"]]["mode"] for feature in original["features"]}
    feature_to_block = {feature["id"]: feature["block_id"] for feature in original["features"]}
    equivalence = validate_equivalence(program, transformed, effective_modes, feature_to_block)
    validation_errors.extend(equivalence["errors"])
    if resulting_dimension is None or abs(resulting_dimension - target_dimension) > 0.05:
        validation_errors.append(f"Actual preview dimension {resulting_dimension} does not match requested {target_dimension}; fixed features may constrain the envelope")
    for block in updated_geometry["blocks"]:
        if block["connectivity_errors"]:
            validation_errors.append(f"{block['block_id']} has disconnected motion segments")
        if block["unresolved_moves"]:
            validation_errors.append(f"{block['block_id']} has unresolved arc/coordinate geometry")
    if program.unknown_g_codes or program.unknown_m_codes or program.compact_controller_tokens:
        validation_warnings.append("Unknown/controller-specific commands remain; machine output is blocked")
    if program.units is None:
        validation_warnings.append("Program units are unknown; dimensions are in source program units")
    if not changed_features and abs(delta) > 1e-9:
        validation_errors.append("No geometry changed despite a nonzero requested resize")

    return {
        "preview_only": True,
        "safe_for_execution": False,
        "source_sha256": program.source_sha256,
        "axis": axis,
        "dimension_before": current_dimension,
        "dimension_requested": target_dimension,
        "dimension_after": resulting_dimension,
        "delta": delta,
        "reference_points": {"start": start, "end": end},
        "constraints": constraints,
        "changed_feature_ids": changed_features,
        "original": original,
        "resized": recognize(transformed),
        "validation": {"passed": not validation_errors, "errors": validation_errors, "warnings": validation_warnings,
                       "controller_approved": False, "machine_output_written": False},
        "program_ir": to_dict(transformed),
    }


def _shift_block_rigid(block: Any, dx: float, dy: float, feature_id: str) -> None:
    """Rigidly translate one parsed block for interactive editing.

    The parser starts a PathBlock exactly at its XY positioning rapid, so the
    block always carries its own tool entry and a rigid shift stays expressible
    as a pure coordinate patch: the entry rapid's END and every XY-bearing cut
    move's END are what the source lines can patch. For a transformed move,
    resolved modal XY values are also shifted; the serializer materializes a
    missing absolute axis word on that same source line. This keeps the
    translated geometry explicit without relying on a prior line's modal side
    effect. Cut moves shift start+end together and arc centres translate with
    them, which keeps every I/J offset identical.
    """
    motions = block.motions
    cut = [move for move in motions if move.motion in ("linear", "arc_cw", "arc_ccw")]
    if not cut:
        raise TransformError(f"Feature {feature_id}: the block has no cutting motion to shift")
    first = cut[0]
    if (first.start is None or first.start.x is None or first.start.y is None
            or first.end is None or first.end.x is None or first.end.y is None):
        raise TransformError(f"Feature {feature_id}: the first cutting move has unresolved coordinates")
    entry = motions[0]
    if entry.motion != "rapid" or entry.machine_coordinates:
        raise TransformError(
            f"Feature {feature_id}: the block does not start with an independent positioning rapid; "
            "shifting it would cut a diagonal from the stale entry position")
    if entry.end is None or entry.end.x is None or entry.end.y is None:
        raise TransformError(f"Feature {feature_id}: the positioning rapid has unresolved coordinates")
    if abs(entry.end.x - first.start.x) > 1e-6 or abs(entry.end.y - first.start.y) > 1e-6:
        raise TransformError(
            f"Feature {feature_id}: the positioning rapid does not end at the first cutting move; "
            "unsupported block structure")
    entry.end.x += dx
    entry.end.y += dy
    # NOTE: the parser aliases block.start to the entry rapid's end Point3
    # (start=end), so block.start is already shifted with entry.end — shifting
    # it again would double the delta.
    for move in motions[1:]:
        if move.motion == "rapid":
            # Z-only rapid inside the block (retract/plunge): XY is modal at
            # runtime and the line carries no X/Y words — leave untouched. An
            # XY-bearing rapid would have started its own PathBlock at parse
            # time, so reaching this branch with X/Y words means an unexpected
            # source shape: fail closed instead of leaving a stale
            # repositioning rapid behind.
            if "x" in (move.raw_words or {}) or "y" in (move.raw_words or {}):
                raise TransformError(
                    f"Feature {feature_id}: an internal repositioning rapid is not supported; "
                    "blocks are expected to contain Z-only rapids only")
            continue
        if move.motion not in ("linear", "arc_cw", "arc_ccw"):
            continue
        if move.end is None or move.end.x is None or move.end.y is None:
            raise TransformError(f"Feature {feature_id}: move {move.id} has unresolved coordinates")
        raw_words = move.raw_words or {}
        # A Z-only plunge/retract must remain Z-only; adding XY here would
        # turn the vertical plunge into a diagonal cut. Its XY position follows
        # the already translated preceding cut move by normal modal carry.
        if not ("x" in raw_words or "y" in raw_words):
            continue
        move.end.x += dx
        move.end.y += dy
        if move.arc is not None:
            # Arcs: start and centre always move. The runtime start is the
            # modal chain position (already shifted) and I/J offsets are
            # relative, so the centre follows the start; keeping both in sync
            # leaves every I/J offset identical.
            if move.start is None or move.start.x is None or move.start.y is None:
                raise TransformError(f"Feature {feature_id}: move {move.id} has unresolved coordinates")
            move.start.x += dx
            move.start.y += dy
            if move.arc.center:
                if "x" in move.arc.center:
                    move.arc.center["x"] += dx
                if "y" in move.arc.center:
                    move.arc.center["y"] += dy


_MIRROR_ARC_G_RE = re.compile(r"(?<![A-Z])G\s*[23](?![.\d])", re.IGNORECASE)
_CUTTER_COMP_RE = re.compile(r"(?<![A-Z])G\s*4[12](?![.\d])", re.IGNORECASE)


def _mirror_feature(block: Any, axis: str, center: float, feature_id: str, flips: list[str]) -> None:
    """Mirror one parsed block across its own centre on one axis.

    The block stays in place (its bounds are preserved); every mirrored-axis
    coordinate reflects around ``center`` and every arc reverses direction
    (``G2 <-> G3``, I/J offset sign flips on the mirrored axis). The entry
    rapid must carry the mirrored axis explicitly, and every arc line must
    carry an explicit G2/G3 word — otherwise the flip cannot be expressed as
    a text patch and the edit fails closed. Cutter compensation flips sides
    under mirroring and is rejected outright.
    """
    motions = block.motions
    cut = [move for move in motions if move.motion in ("linear", "arc_cw", "arc_ccw")]
    if not cut:
        raise TransformError(f"Feature {feature_id}: the block has no cutting motion to mirror")
    entry = motions[0]
    if entry.motion != "rapid" or entry.machine_coordinates:
        raise TransformError(
            f"Feature {feature_id}: the block does not start with an independent positioning rapid; "
            "mirroring is not supported")
    if entry.end is None or entry.end.x is None or entry.end.y is None:
        raise TransformError(f"Feature {feature_id}: the positioning rapid has unresolved coordinates")
    for move in motions:
        if _CUTTER_COMP_RE.search(move.raw_line or ""):
            raise TransformError(
                f"Feature {feature_id}: cutter compensation (G41/G42) flips tool side under "
                "mirroring; mirroring a compensated block is not supported")
    entry_words = entry.raw_words or {}
    if axis not in entry_words:
        raise TransformError(
            f"Feature {feature_id}: the entry rapid line carries no {axis.upper()} word; "
            "the mirrored entry position cannot be patched")
    if axis == "x":
        entry.end.x = 2 * center - entry.end.x
    else:
        entry.end.y = 2 * center - entry.end.y
    for move in motions[1:]:
        raw_words = move.raw_words or {}
        if move.motion == "rapid":
            if "x" in raw_words or "y" in raw_words:
                raise TransformError(
                    f"Feature {feature_id}: an internal repositioning rapid is not supported; "
                    "blocks are expected to contain Z-only rapids only")
            continue
        if move.motion not in ("linear", "arc_cw", "arc_ccw"):
            continue
        if move.end is None or move.end.x is None or move.end.y is None:
            raise TransformError(f"Feature {feature_id}: move {move.id} has unresolved coordinates")
        if axis in raw_words:
            if axis == "x":
                move.end.x = 2 * center - move.end.x
            else:
                move.end.y = 2 * center - move.end.y
        if move.arc is not None:
            if move.start is None or move.start.x is None or move.start.y is None:
                raise TransformError(f"Feature {feature_id}: move {move.id} has unresolved coordinates")
            if not _MIRROR_ARC_G_RE.search(move.raw_line or ""):
                raise TransformError(
                    f"Feature {feature_id}: arc move {move.id} carries no explicit G2/G3 word on its "
                    "line; mirroring cannot flip the arc direction and fails closed")
            if move.arc.center and axis in move.arc.center:
                if axis == "x":
                    move.arc.center["x"] = 2 * center - move.arc.center["x"]
                else:
                    move.arc.center["y"] = 2 * center - move.arc.center["y"]
            if axis == "x":
                move.start.x = 2 * center - move.start.x
            else:
                move.start.y = 2 * center - move.start.y
            move.motion = "arc_ccw" if move.motion == "arc_cw" else "arc_cw"
            move.arc.direction = "ccw" if move.arc.direction == "cw" else "cw"
            flips.append(move.id)


def _rotate_feature(block: Any, direction: str, center_x: float, center_y: float,
                    feature_id: str) -> None:
    """Rotate one parsed block by exactly 90 degrees around its own centre.

    Rotation is orientation-preserving (det = +1), so — unlike mirroring — the
    arc direction (G2/G3) and the cutter-compensation side (G41/G42) both stay
    valid and untouched; only coordinates move. Relative to the centre a CCW
    rotation maps ``(dx, dy) -> (-dy, dx)`` and a CW rotation maps
    ``(dx, dy) -> (dy, -dx)``; the arc centre offset (I, J) rotates by the
    same rule. Every cut line of a rotated block must end up carrying BOTH
    axis words explicitly (the serializer inserts a missing X/Y word next to
    the existing ones and, for arcs, a missing I/J), so the candidate IR holds
    the true rotated end coordinates on both axes. Arc lines must carry both I
    and J words up front: a missing centre offset is an invalid arc for the
    parser anyway and rotation cannot express a half-resolved centre. Z-only
    modal carries inside the block stay untouched exactly like a rigid shift.
    """
    motions = block.motions
    cut = [move for move in motions if move.motion in ("linear", "arc_cw", "arc_ccw")]
    if not cut:
        raise TransformError(f"Feature {feature_id}: the block has no cutting motion to rotate")
    entry = motions[0]
    if entry.motion != "rapid" or entry.machine_coordinates:
        raise TransformError(
            f"Feature {feature_id}: the block does not start with an independent positioning rapid; "
            "rotating it would cut a diagonal from the stale entry position")
    if entry.end is None or entry.end.x is None or entry.end.y is None:
        raise TransformError(f"Feature {feature_id}: the positioning rapid has unresolved coordinates")
    entry_words = entry.raw_words or {}
    if "x" not in entry_words or "y" not in entry_words:
        raise TransformError(
            f"Feature {feature_id}: the entry rapid line must carry both X and Y words so the "
            "rotated entry position is fully explicit on its own line")
    for move in motions[1:]:
        raw_words = move.raw_words or {}
        if move.motion == "rapid":
            if "x" in raw_words or "y" in raw_words:
                raise TransformError(
                    f"Feature {feature_id}: an internal repositioning rapid is not supported; "
                    "blocks are expected to contain Z-only rapids only")
            continue
        if move.motion not in ("linear", "arc_cw", "arc_ccw"):
            continue
        if move.end is None or move.end.x is None or move.end.y is None:
            raise TransformError(f"Feature {feature_id}: move {move.id} has unresolved coordinates")
        if move.arc is not None and ("i" not in raw_words or "j" not in raw_words):
            raise TransformError(
                f"Feature {feature_id}: arc move {move.id} must carry both I and J words on its line; "
                "rotation re-expresses both centre offsets and fails closed on a half-resolved arc")

    def rotate_point(x: float, y: float) -> tuple[float, float]:
        dx, dy = x - center_x, y - center_y
        if direction == "ccw":
            return center_x - dy, center_y + dx
        return center_x + dy, center_y - dx

    entry.end.x, entry.end.y = rotate_point(entry.end.x, entry.end.y)
    # NOTE: the parser aliases block.start to the entry rapid's end Point3
    # (start=end), so block.start is already rotated with entry.end — never
    # rotate it again.
    for move in motions[1:]:
        if move.motion not in ("linear", "arc_cw", "arc_ccw"):
            continue
        raw_words = move.raw_words or {}
        # A line carrying at least one axis word ends up fully explicit in the
        # patched output (existing word patched, missing word inserted), so
        # both of its end axes get the true rotated values. A line with NO
        # X/Y word (a Z-only plunge/retract) stays byte-identical and its
        # endpoint follows the modal chain at runtime — its static IR end
        # stays stale on purpose, exactly like a rigid shift leaves it.
        if "x" in raw_words or "y" in raw_words:
            move.end.x, move.end.y = rotate_point(move.end.x, move.end.y)
        if move.arc is not None:
            if move.start is None or move.start.x is None or move.start.y is None:
                raise TransformError(f"Feature {feature_id}: move {move.id} has unresolved coordinates")
            move.start.x, move.start.y = rotate_point(move.start.x, move.start.y)
            if move.arc.center and "x" in move.arc.center and "y" in move.arc.center:
                move.arc.center["x"], move.arc.center["y"] = rotate_point(
                    move.arc.center["x"], move.arc.center["y"])


def _stretch_feature_x(block: Any, feature_id: str, band_start: float, band_end: float,
                       below_offset: float, above_offset: float) -> None:
    """Segment-level X stretch of one parsed block for interactive editing.

    The band is the feature's own X span. Everything wholly left of
    ``band_start`` / wholly right of ``band_end`` stays rigid (translated by
    ``below_offset`` / ``above_offset`` according to the chosen anchor); only
    straight moves parallel to X absorb the length change. A move whose two
    junction offsets differ must be an axial straight line with an X word —
    an arc would be distorted, a Y-bearing line would be skewed, and a line
    without an X word cannot carry the new coordinate — each is rejected
    instead of being guessed at.

    Offsets are decided per *junction* from the untouched coordinates: left of
    the band keeps ``below_offset``, right of it keeps ``above_offset``, a
    junction strictly inside stays put so the axial move next to it absorbs
    the whole stretch. Because the rule is a pure function of the junction X,
    connected chains — including Z-only modal carries between cut moves — and
    closed-loop wrap points stay consistent automatically.
    """
    tol = 1e-6
    motions = block.motions
    moves = [move for move in motions if move.motion in ("linear", "arc_cw", "arc_ccw")]
    if not moves:
        raise TransformError(f"Feature {feature_id}: the block has no cutting motion to resize")
    first = moves[0]
    if (first.start is None or first.start.x is None or first.start.y is None
            or first.end is None or first.end.x is None or first.end.y is None):
        raise TransformError(f"Feature {feature_id}: the first cutting move has unresolved coordinates")
    entry = motions[0]
    if entry.motion != "rapid" or entry.machine_coordinates:
        raise TransformError(
            f"Feature {feature_id}: the block does not start with an independent positioning rapid; "
            "resizing it would cut a diagonal from the stale entry position")
    if entry.end is None or entry.end.x is None or entry.end.y is None:
        raise TransformError(f"Feature {feature_id}: the positioning rapid has unresolved coordinates")
    if abs(entry.end.x - first.start.x) > 1e-6 or abs(entry.end.y - first.start.y) > 1e-6:
        raise TransformError(
            f"Feature {feature_id}: the positioning rapid does not end at the first cutting move; "
            "unsupported block structure")
    for move in motions[1:]:
        if move.motion == "rapid" and ("x" in (move.raw_words or {}) or "y" in (move.raw_words or {})):
            # Unreachable by parser construction (an XY-bearing rapid starts
            # its own PathBlock); kept fail-closed for defense in depth.
            raise TransformError(
                f"Feature {feature_id}: an internal repositioning rapid is not supported; "
                "blocks are expected to contain Z-only rapids only")

    junctions = [moves[0].start.x] + [move.end.x for move in moves]
    if any(value is None for value in junctions):
        raise TransformError(f"Feature {feature_id}: unresolved X coordinates in the motion chain")
    if min(junctions) > band_start + tol or max(junctions) < band_end - tol:
        raise TransformError(
            f"Feature {feature_id}: the feature's X extremes include arc bulges rather than move "
            "endpoints; segment-level X stretch needs endpoints at both extremes "
            "(arc-ended shapes are not stretchable in phase one)")

    def junction_offset(value: float) -> float:
        if value <= band_start + tol:
            return below_offset
        if value >= band_end - tol:
            return above_offset
        return 0.0

    offsets = [junction_offset(value) for value in junctions]

    previous_end = None
    for index, move in enumerate(moves):
        if (move.start is None or move.end is None or move.start.x is None or move.end.x is None
                or move.start.y is None or move.end.y is None):
            raise TransformError(f"Feature {feature_id}: move {move.id} has unresolved coordinates")
        if previous_end is not None and (
                abs(move.start.x - previous_end[0]) > 1e-6 or abs(move.start.y - previous_end[1]) > 1e-6):
            raise TransformError(f"Feature {feature_id}: disconnected motion chain before move {move.id}")
        previous_end = (move.end.x, move.end.y)
        if abs(offsets[index] - offsets[index + 1]) > 1e-9:
            if move.motion != "linear":
                raise TransformError(
                    f"Feature {feature_id}: resizing keeps arcs rigid; an arc would need different "
                    "offsets at its two ends and would be distorted")
            if abs(move.end.y - move.start.y) > tol:
                raise TransformError(
                    f"Feature {feature_id}: resizing lengthens only straight moves parallel to X; "
                    "a move with a Y change would be skewed")
            if "x" not in (move.raw_words or {}):
                raise TransformError(
                    f"Feature {feature_id}: move {move.id} would need a new X but its line has no X "
                    "word; the stretch cannot be expressed as a coordinate patch")

    if abs(offsets[0]) > 1e-9:
        entry.end.x += offsets[0]
        # NOTE: the parser aliases block.start to the entry rapid's end Point3
        # (start=end), so block.start already moved with entry.end — never
        # shift it again.
    for index, move in enumerate(moves):
        start_offset, end_offset = offsets[index], offsets[index + 1]
        if move.motion == "rapid":
            continue  # Z-only modal carry: the line has no X/Y words
        if "x" in (move.raw_words or {}) and abs(end_offset) > 1e-12:
            move.end.x += end_offset
        if move.arc is not None:
            # Arcs never straddle the offsets (rejected above): start, end and
            # centre translate by the same offset, so every I/J offset stays
            # identical and the serializer never needs to touch I/J words.
            if move.start is None or move.start.x is None:
                raise TransformError(f"Feature {feature_id}: move {move.id} has unresolved coordinates")
            if abs(start_offset) > 1e-12:
                move.start.x += start_offset
                if move.arc.center and "x" in move.arc.center:
                    move.arc.center["x"] += start_offset


_MAX_GRID_COPIES = 64  # mirrors serializer._MAX_COPIES_PER_BLOCK


def preview_edits(program: Program, request: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Interactive rigid per-feature edits (phases one to three plus the
    package-F grid: shift, X stretch, declarative placement constraints,
    linear arrays, 2-D grids, mirroring).

    A ``shift`` edit translates one recognized feature's whole block by
    ``(dx, dy)``; a ``resize_x`` edit stretches one feature's own X span to
    ``new_length`` around an anchor (``min``/``max``/``center``). Phase two
    adds three constraint ops that are resolved against the ORIGINAL geometry
    into derived rigid shifts: ``center_on`` (feature or program-envelope
    centre), ``set_gap`` (exact edge-to-edge gap to an anchor feature on one
    axis) and ``center_between`` (equidistant midpoint of two anchor
    features). Phase three adds ``array`` (``count`` translated copies of the
    feature's block, inserted right after it as text duplications) and
    ``mirror`` (the block reflected across its own centre, arcs reversed).
    Package F adds ``grid``: ``count_x`` x ``count_y`` translated copies
    (cartesian product of the two signed pitches, row-major order) forming an
    ``(count_x+1) x (count_y+1)`` arrangement with the untouched original at
    its corner. A constraint or an array/grid/mirror/rotate on a member of a
    depth-repeat group broadcasts to every member. The candidate is accepted
    only after the serializer accepts it AND a runtime reparse of the patched
    draft proves every move (original or copy) reached exactly its expected
    position while no Z value changed, and no edit CREATED a new partial
    bounds overlap between two features (overlaps already present in the
    source stay the operator's responsibility; full containment counts as
    legitimate nesting). Features sharing the same XY path at multiple
    depths must be edited together with identical parameters or not at all.

    ``rotate`` turns one feature by exactly 90 degrees around its own bounds
    centre (``dir`` = ``cw``/``ccw``). Rotation preserves orientation, so arc
    directions and cutter-compensation sides never change; the serializer
    inserts missing X/Y (and arc I/J) words so every rotated cut line ends up
    fully explicit, and the width/height of the feature swap around the same
    centre.
    """
    if not isinstance(request, dict):
        raise TransformError("Edits request must be an object")
    edits = request.get("edits")
    if not isinstance(edits, list) or not edits:
        raise TransformError("edits must be a non-empty list")
    recognition = recognize(program)
    feature_by_id = {feature["id"]: feature for feature in recognition["features"]}
    normalized: list[dict[str, Any]] = []
    raw_constraints: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, edit in enumerate(edits):
        label = f"Edit {index + 1}"
        if not isinstance(edit, dict) or edit.get("op") not in ("shift", "resize_x", "array", "grid", "mirror",
                                                                "rotate", "center_on", "set_gap", "center_between"):
            raise TransformError(
                f"{label}: op must be one of shift, resize_x, array, grid, mirror, rotate, "
                "center_on, set_gap, center_between")
        feature_id = edit.get("feature_id")
        if not isinstance(feature_id, str) or feature_id not in feature_by_id:
            raise TransformError(f"{label}: unknown feature_id {feature_id!r}")
        if feature_id in seen:
            raise TransformError(f"{label}: feature {feature_id} is edited twice")
        seen.add(feature_id)
        feature = feature_by_id[feature_id]
        if not feature.get("bounds"):
            raise TransformError(f"{label}: feature {feature_id} has unresolved geometry bounds")
        if edit["op"] == "shift":
            dx = _finite_number(edit.get("dx"), f"{label} dx")
            dy = _finite_number(edit.get("dy"), f"{label} dy")
            if dx == 0 and dy == 0:
                raise TransformError(f"{label}: shift distance is zero")
            normalized.append({"op": "shift", "feature_id": feature_id, "dx": dx, "dy": dy,
                               "block_id": feature["block_id"]})
        elif edit["op"] == "mirror":
            axis = edit.get("axis")
            if axis not in ("x", "y"):
                raise TransformError(f"{label}: mirror axis must be 'x' or 'y'")
            bounds = feature["bounds"]
            low, high = axis + "_min", axis + "_max"
            if bounds[high] - bounds[low] <= 1e-6:
                raise TransformError(
                    f"{label}: mirroring around the feature's own centre is a no-op on "
                    f"{axis.upper()} for feature {feature_id} (no {axis.upper()} extent)")
            normalized.append({"op": "mirror", "feature_id": feature_id, "axis": axis,
                               "center": (bounds[low] + bounds[high]) / 2,
                               "block_id": feature["block_id"]})
        elif edit["op"] == "rotate":
            direction = edit.get("dir")
            if direction not in ("cw", "ccw"):
                raise TransformError(f"{label}: rotation direction must be 'cw' or 'ccw'")
            bounds = feature["bounds"]
            if (bounds["x_max"] - bounds["x_min"] <= 1e-6
                    and bounds["y_max"] - bounds["y_min"] <= 1e-6):
                raise TransformError(
                    f"{label}: rotating feature {feature_id} around its own centre is a no-op "
                    "(no extent on either axis)")
            normalized.append({"op": "rotate", "feature_id": feature_id, "dir": direction,
                               "center_x": (bounds["x_min"] + bounds["x_max"]) / 2,
                               "center_y": (bounds["y_min"] + bounds["y_max"]) / 2,
                               "block_id": feature["block_id"]})
        elif edit["op"] == "array":
            axis = edit.get("axis")
            if axis not in ("x", "y"):
                raise TransformError(f"{label}: array axis must be 'x' or 'y'")
            count = edit.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 64:
                raise TransformError(
                    f"{label}: count must be an integer between 1 and 64 (copies to add)")
            pitch = _finite_number(edit.get("pitch"), f"{label} pitch")
            if abs(pitch) <= 1e-6:
                raise TransformError(f"{label}: pitch must not be zero")
            deltas = []
            for step in range(1, count + 1):
                offset = pitch * step
                deltas.append({"dx": offset if axis == "x" else 0.0,
                               "dy": offset if axis == "y" else 0.0})
            normalized.append({"op": "array", "feature_id": feature_id, "axis": axis,
                               "count": count, "pitch": pitch, "deltas": deltas,
                               "block_id": feature["block_id"]})
        elif edit["op"] == "grid":
            count_x = edit.get("count_x")
            count_y = edit.get("count_y")
            for name, value in (("count_x", count_x), ("count_y", count_y)):
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
                    raise TransformError(
                        f"{label}: {name} must be an integer between 1 and 32 (copies to add on that axis)")
            if count_x * count_y < 2:
                raise TransformError(
                    f"{label}: a grid needs at least one copy; count_x * count_y must be 2 or more "
                    "(for a single row use op=array)")
            if count_x * count_y > _MAX_GRID_COPIES:
                raise TransformError(
                    f"{label}: at most {_MAX_GRID_COPIES} copies per feature are supported "
                    f"(count_x * count_y = {count_x * count_y})")
            pitch_x = 0.0
            pitch_y = 0.0
            if count_x > 1:
                pitch_x = _finite_number(edit.get("pitch_x"), f"{label} pitch_x")
                if abs(pitch_x) <= 1e-6:
                    raise TransformError(f"{label}: pitch_x must not be zero when count_x > 1")
            if count_y > 1:
                pitch_y = _finite_number(edit.get("pitch_y"), f"{label} pitch_y")
                if abs(pitch_y) <= 1e-6:
                    raise TransformError(f"{label}: pitch_y must not be zero when count_y > 1")
            # Row-major order (Y rows outer, X columns inner): copies land
            # row by row next to their source block.
            deltas = [{"dx": pitch_x * i, "dy": pitch_y * j}
                      for j in range(1, count_y + 1) for i in range(1, count_x + 1)]
            normalized.append({"op": "grid", "feature_id": feature_id,
                               "count_x": count_x, "count_y": count_y,
                               "pitch_x": pitch_x, "pitch_y": pitch_y, "deltas": deltas,
                               "block_id": feature["block_id"]})
        elif edit["op"] == "resize_x":
            new_length = _finite_number(edit.get("new_length"), f"{label} new_length")
            if new_length <= 0:
                raise TransformError(f"{label}: new_length must be greater than zero")
            anchor = edit.get("anchor", "min")
            if anchor not in ("min", "max", "center"):
                raise TransformError(f"{label}: anchor must be one of 'min', 'max', 'center'")
            bounds = feature["bounds"]
            span = bounds["x_max"] - bounds["x_min"]
            if span <= 1e-6:
                raise TransformError(f"{label}: feature {feature_id} has no X extent to resize")
            if abs(new_length - span) <= 1e-6:
                raise TransformError(
                    f"{label}: new_length {new_length} matches the feature's current X span "
                    f"({span:.3f}); nothing would change")
            normalized.append({"op": "resize_x", "feature_id": feature_id, "new_length": new_length,
                               "anchor": anchor, "block_id": feature["block_id"],
                               "band_start": bounds["x_min"], "band_end": bounds["x_max"]})
        else:
            # Declarative placement constraint (phase two). Validated here,
            # resolved into a derived rigid shift below against the ORIGINAL
            # geometry — the reference positions never move under the edit.
            axis = edit.get("axis")
            if axis not in ("x", "y"):
                raise TransformError(f"{label}: axis must be 'x' or 'y'")
            anchor_id = edit.get("anchor_feature_id")
            envelope = edit.get("anchor") == "envelope"
            if edit.get("anchor") not in (None, "envelope"):
                raise TransformError(f"{label}: anchor must be 'envelope' when given")
            if envelope and anchor_id is not None:
                raise TransformError(f"{label}: choose an anchor_feature_id or the program envelope, not both")
            if anchor_id is not None:
                if not isinstance(anchor_id, str) or anchor_id not in feature_by_id:
                    raise TransformError(f"{label}: unknown anchor_feature_id {anchor_id!r}")
                if anchor_id == feature_id:
                    raise TransformError(f"{label}: the feature cannot be anchored to itself")
                if not feature_by_id[anchor_id].get("bounds"):
                    raise TransformError(f"{label}: anchor feature {anchor_id} has unresolved geometry bounds")
            entry: dict[str, Any] = {"op": edit["op"], "feature_id": feature_id, "axis": axis}
            if edit["op"] == "center_on":
                if anchor_id is None and not envelope:
                    raise TransformError(f"{label}: center_on needs anchor_feature_id or anchor='envelope'")
            else:
                if anchor_id is None or envelope:
                    raise TransformError(f"{label}: {edit['op']} needs an anchor_feature_id (no envelope anchor)")
            if edit["op"] == "set_gap":
                side = edit.get("side")
                if side not in ("after", "before"):
                    raise TransformError(
                        f"{label}: side must be 'after' or 'before' (which side of the anchor the feature sits on)")
                gap = _finite_number(edit.get("gap"), f"{label} gap")
                if gap < 0:
                    raise TransformError(f"{label}: gap must not be negative; overlap is rejected, not scheduled")
                entry["side"] = side
                entry["gap"] = gap
            if edit["op"] == "center_between":
                second = edit.get("second_feature_id")
                if not isinstance(second, str) or second not in feature_by_id:
                    raise TransformError(f"{label}: unknown second_feature_id {second!r}")
                if second == feature_id:
                    raise TransformError(f"{label}: second_feature_id must differ from the moved feature")
                if second == anchor_id:
                    raise TransformError(f"{label}: the two anchors of center_between must differ")
                if not feature_by_id[second].get("bounds"):
                    raise TransformError(f"{label}: second_feature_id {second} has unresolved geometry bounds")
                entry["second_feature_id"] = second
            if anchor_id is not None:
                entry["anchor_feature_id"] = anchor_id
            if envelope:
                entry["anchor"] = "envelope"
            raw_constraints.append(entry)

    # ---- depth-repeat groups; array/mirror broadcast to every member ----
    group_of: dict[str, list[str]] = {}
    for pattern in recognition["patterns"]:
        if pattern["kind"] == "same_xy_path_at_multiple_depths":
            ids = list(pattern["feature_ids"])
            for fid in ids:
                group_of[fid] = ids
    taken: set[str] = {item["feature_id"] for item in normalized}
    array_summaries: list[dict[str, Any]] = []
    for item in [entry for entry in normalized if entry["op"] in ("array", "grid")]:
        fid = item["feature_id"]
        targets = list(group_of.get(fid, [fid]))
        for member in targets:
            if member != fid and (member in seen or member in taken):
                raise TransformError(
                    f"{item['op'].title()} on feature {fid}: depth-repeat group member {member} is "
                    "already edited; a group replicates only as one unit and every member must be free")
        copy_targets = []
        for member in targets:
            member_feature = feature_by_id[member]
            if not member_feature.get("bounds"):
                raise TransformError(
                    f"{item['op'].title()} on feature {fid}: group member {member} has unresolved geometry bounds")
            copy_targets.append({"feature_id": member, "block_id": member_feature["block_id"]})
            seen.add(member)
            taken.add(member)
        item["copy_targets"] = copy_targets
        summary: dict[str, Any] = {"op": item["op"], "feature_id": fid}
        if item["op"] == "array":
            summary.update(axis=item["axis"], count=item["count"], pitch=item["pitch"])
        else:
            summary.update(count_x=item["count_x"], count_y=item["count_y"],
                           pitch_x=item["pitch_x"], pitch_y=item["pitch_y"])
        summary["targets"] = targets
        array_summaries.append(summary)
    mirror_seeds = [entry for entry in normalized if entry["op"] == "mirror"]
    for item in mirror_seeds:
        fid = item["feature_id"]
        axis = item["axis"]
        for member in group_of.get(fid, [fid]):
            if member == fid:
                continue
            if member in seen or member in taken:
                raise TransformError(
                    f"Mirror on feature {fid}: depth-repeat group member {member} is already edited; "
                    "a group mirrors only as one unit and every member must be free")
        for member in group_of.get(fid, [fid]):
            if member == fid:
                continue
            member_feature = feature_by_id[member]
            bounds = member_feature["bounds"]
            low, high = axis + "_min", axis + "_max"
            if bounds[high] - bounds[low] <= 1e-6:
                raise TransformError(
                    f"Mirror on feature {fid}: group member {member} has no {axis.upper()} extent")
            seen.add(member)
            taken.add(member)
            normalized.append({"op": "mirror", "feature_id": member, "axis": axis,
                               "center": (bounds[low] + bounds[high]) / 2,
                               "block_id": member_feature["block_id"],
                               "derived_from": f"mirror:{fid}"})
    rotate_seeds = [entry for entry in normalized if entry["op"] == "rotate"]
    for item in rotate_seeds:
        fid = item["feature_id"]
        direction = item["dir"]
        for member in group_of.get(fid, [fid]):
            if member == fid:
                continue
            if member in seen or member in taken:
                raise TransformError(
                    f"Rotation on feature {fid}: depth-repeat group member {member} is already "
                    "edited; a group rotates only as one unit and every member must be free")
        for member in group_of.get(fid, [fid]):
            if member == fid:
                continue
            member_feature = feature_by_id[member]
            bounds = member_feature["bounds"]
            if (bounds["x_max"] - bounds["x_min"] <= 1e-6
                    and bounds["y_max"] - bounds["y_min"] <= 1e-6):
                raise TransformError(
                    f"Rotation on feature {fid}: group member {member} has no extent on either axis")
            seen.add(member)
            taken.add(member)
            normalized.append({"op": "rotate", "feature_id": member, "dir": direction,
                               "center_x": (bounds["x_min"] + bounds["x_max"]) / 2,
                               "center_y": (bounds["y_min"] + bounds["y_max"]) / 2,
                               "block_id": member_feature["block_id"],
                               "derived_from": f"rotate:{fid}"})
    # ---- phase two: resolve constraints into derived rigid shifts ----
    constraints_applied: list[dict[str, Any]] = []
    for index, constraint in enumerate(raw_constraints):
        cid = f"constraint-{index + 1}"
        fid = constraint["feature_id"]
        axis = constraint["axis"]
        low, high = axis + "_min", axis + "_max"
        feature_bounds = feature_by_id[fid]["bounds"]
        feature_measure = (feature_bounds[low] + feature_bounds[high]) / 2
        op = constraint["op"]
        if op == "center_on" and constraint.get("anchor") == "envelope":
            envelope_bounds = recognition.get("bounds")
            if not envelope_bounds:
                raise TransformError(f"{cid}: the program envelope is unavailable")
            anchor_measure = (envelope_bounds[low] + envelope_bounds[high]) / 2
            translation = anchor_measure - feature_measure
        else:
            anchor_bounds = feature_by_id[constraint["anchor_feature_id"]]["bounds"]
            anchor_measure = (anchor_bounds[low] + anchor_bounds[high]) / 2
            if op == "center_on":
                translation = anchor_measure - feature_measure
            elif op == "set_gap":
                gap = constraint["gap"]
                translation = (anchor_bounds[high] + gap - feature_bounds[low] if constraint["side"] == "after"
                               else anchor_bounds[low] - gap - feature_bounds[high])
            else:  # center_between: equidistant midpoint of the two anchors
                second_bounds = feature_by_id[constraint["second_feature_id"]]["bounds"]
                second_measure = (second_bounds[low] + second_bounds[high]) / 2
                translation = (anchor_measure + second_measure) / 2 - feature_measure
        dx, dy = (translation, 0.0) if axis == "x" else (0.0, translation)
        if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
            raise TransformError(f"{cid}: the constraint already holds; nothing would change")
        targets = list(group_of.get(fid, [fid]))
        for member in targets:
            if member in taken:
                raise TransformError(
                    f"{cid}: feature {member} is already moved by another edit; a depth-repeat group "
                    "moves only as one unit and every feature takes at most one edit")
        for member in targets:
            normalized.append({"op": "shift", "feature_id": member, "dx": dx, "dy": dy,
                               "block_id": feature_by_id[member]["block_id"],
                               "derived_from": f"{cid}:{op}"})
            taken.add(member)
        record: dict[str, Any] = {"id": cid, "rule": op, "feature_id": fid, "axis": axis,
                                  "translation": {"dx": dx, "dy": dy}, "targets": targets,
                                  "evaluated_against": "original_geometry"}
        if constraint.get("anchor") == "envelope":
            record["anchor"] = "envelope"
        else:
            record["anchor_feature_id"] = constraint["anchor_feature_id"]
            if op == "center_between":
                record["second_feature_id"] = constraint["second_feature_id"]
            if op == "set_gap":
                record["side"] = constraint["side"]
                record["gap"] = constraint["gap"]
        constraints_applied.append(record)

    edit_by_feature = {item["feature_id"]: item for item in normalized}

    def edit_signature(item: dict[str, Any]) -> tuple:
        return tuple(sorted((key, value) for key, value in item.items()
                            if key not in ("feature_id", "block_id", "copy_targets", "derived_from")))

    for pattern in recognition["patterns"]:
        if pattern["kind"] != "same_xy_path_at_multiple_depths":
            continue
        group = list(pattern["feature_ids"])
        touched = [fid for fid in group if fid in edit_by_feature
                   and edit_by_feature[fid]["op"] not in ("array", "grid")]
        if not touched:
            continue
        if len(touched) < len(group):
            raise TransformError(
                "Features " + ", ".join(sorted(group)) + " share the same XY path at multiple depths; "
                "apply the identical edit to all of them or none of them")
        reference = edit_signature(edit_by_feature[touched[0]])
        if any(edit_signature(edit_by_feature[fid]) != reference for fid in touched):
            reference_op = dict(reference).get("op") if reference else None
            detail = {"shift": "identical shift delta", "resize_x": "identical resize_x parameters",
                      "rotate": "identical rotation direction"}.get(reference_op, "identical parameters")
            raise TransformError(
                "Features sharing the same XY path at multiple depths must receive the " + detail)

    transformed = copy.deepcopy(program)
    blocks = {block.id: block for block in transformed.blocks}
    mirror_flips: list[str] = []
    for item in normalized:
        block = blocks.get(item["block_id"])
        if block is None:
            raise TransformError(f"Feature {item['feature_id']}: source block vanished")
        if item["op"] == "shift":
            _shift_block_rigid(block, item["dx"], item["dy"], item["feature_id"])
        elif item["op"] == "mirror":
            _mirror_feature(block, item["axis"], item["center"], item["feature_id"], mirror_flips)
        elif item["op"] == "rotate":
            _rotate_feature(block, item["dir"], item["center_x"], item["center_y"], item["feature_id"])
        elif item["op"] == "resize_x":
            delta = item["new_length"] - (item["band_end"] - item["band_start"])
            below, above = {"min": (0.0, delta), "max": (-delta, 0.0),
                            "center": (-delta / 2.0, delta / 2.0)}[item["anchor"]]
            _stretch_feature_x(block, item["feature_id"], item["band_start"], item["band_end"],
                               below, above)
        # array: the originals stay put; the copies are text-level block
        # duplications handled by the serializer (block_copies below)

    block_copies: list[dict[str, Any]] = []
    for item in normalized:
        if item["op"] not in ("array", "grid"):
            continue
        for target in item["copy_targets"]:
            block_copies.append({"source_block_id": target["block_id"], "deltas": item["deltas"]})

    provisional = {"validation": {"passed": True, "errors": [], "warnings": []},
                   "source_sha256": program.source_sha256,
                   "program_ir": to_dict(transformed)}
    if block_copies:
        provisional["block_copies"] = block_copies
    if mirror_flips:
        provisional["arc_direction_flips"] = mirror_flips
    rotated_block_ids = {item["block_id"] for item in normalized if item["op"] == "rotate"}
    rotated_move_ids = [move.id for block_id in sorted(rotated_block_ids)
                        for move in blocks[block_id].motions] if rotated_block_ids else []
    if rotated_move_ids:
        provisional["rotated_move_ids"] = rotated_move_ids
    try:
        draft = serialize_candidate(source_text, provisional)
    except SerializerBlocked as exc:
        blockers = getattr(exc, "blockers", None) or [str(exc)]
        raise TransformError(
            "Interactive edit cannot be expressed as a safe coordinate patch: " + "؛ ".join(blockers)) from exc

    # The proof compares runtime values (parsed back from the patched TEXT)
    # against exact transformed-IR values. The patch writes 3-decimal literals
    # (serializer._format_value), so every patched axis word sits up to 5e-4
    # off its exact value, and an arc centre (snapped start + snapped I/J)
    # up to 1e-3 — the proof must not demand more precision than the text
    # format can carry. Real structural corruption (wrong word patched, lost
    # move, modal break) fails at O(1) mm scale, orders above this cutoff.
    # 1e-6 here made array/grid/mirror/rotate fail-closed on real workshop
    # files whose coordinates are not 3-decimal-exact (discovered live with a
    # manual panel file: rotate rejected with 3e-4 runtime deltas).
    tolerance = 1.5e-3
    errors: list[str] = []
    runtime = parse_program(draft["text"])
    runtime_recognition = recognize(runtime)
    runtime_features = {feature["id"]: feature for feature in runtime_recognition["features"]}
    has_copies = bool(block_copies)
    has_mirror = bool(mirror_flips)
    has_rotated = bool(rotated_block_ids)

    # ---- expected-block / expected-move walk (arrays and mirrors) ----
    # Every runtime move must equal the transformed move (or the transformed
    # move plus its copy delta), with modal carries resolved the same way the
    # parser resolves them. This is the strongest proof available: it pins
    # every end coordinate, every arc centre, every motion word and every Z
    # of the whole patched draft, copy or original.
    block_map: dict[str, Any] = {}
    copy_blocks: list[tuple[Any, tuple[float, float], Any]] = []
    if has_copies or has_mirror or has_rotated:
        copy_plan: dict[str, list[tuple[float, float]]] = {}
        for item in normalized:
            if item["op"] in ("array", "grid"):
                for target in item["copy_targets"]:
                    copy_plan.setdefault(target["block_id"], []).extend(
                        (delta["dx"], delta["dy"]) for delta in item["deltas"])
        expected_count = len(program.blocks) + sum(len(deltas) for deltas in copy_plan.values())
        if len(runtime.blocks) != expected_count:
            errors.append("Patched draft changed the block structure "
                          f"({len(runtime.blocks)} runtime blocks vs {expected_count} expected)")
        expected_moves: list[dict[str, Any]] = []
        position: dict[str, Any] = {"x": None, "y": None, "z": None}
        last_move_of_block = {block.id: (block.motions[-1].id if block.motions else None)
                              for block in transformed.blocks}
        pending_copies = {block_id: list(deltas) for block_id, deltas in copy_plan.items()}

        def _emit(move: Any, ddx: float, ddy: float) -> None:
            if move.machine_coordinates:
                # G53 moves live in the machine frame (work-frame unknown):
                # only the motion word is comparable
                expected_moves.append({"motion": move.motion, "end": None,
                                       "center": None, "id": move.id})
                return
            words = move.raw_words or {}
            # A rotated block's cut lines that already carried at least one
            # axis word end up explicit on BOTH axes (existing word patched,
            # missing word inserted), so both axes resolve from the candidate
            # IR. Z-only lines (plunges) stay modal exactly like a shift.
            rotated_explicit = (move.source_block in rotated_block_ids
                                and ("x" in words or "y" in words))
            end_expected = {}
            for axis in ("x", "y", "z"):
                axis_delta = ddx if axis == "x" else ddy if axis == "y" else 0.0
                explicit = axis in words or (rotated_explicit and axis in ("x", "y"))
                if explicit and move.end is not None and getattr(move.end, axis) is not None:
                    end_expected[axis] = getattr(move.end, axis) + axis_delta
                else:
                    end_expected[axis] = position[axis]
            center_expected = None
            if move.arc is not None and move.arc.center:
                center_expected = {}
                for axis in ("x", "y"):
                    axis_delta = ddx if axis == "x" else ddy
                    if move.arc.center.get(axis) is not None:
                        center_expected[axis] = move.arc.center[axis] + axis_delta
            expected_moves.append({"motion": move.motion, "end": end_expected,
                                   "center": center_expected, "id": move.id})
            position.clear()
            position.update(end_expected)

        for move in transformed.moves:
            _emit(move, 0.0, 0.0)
            if last_move_of_block.get(move.source_block) == move.id:
                for ddx, ddy in pending_copies.get(move.source_block, []):
                    for copy_move in blocks[move.source_block].motions:
                        _emit(copy_move, ddx, ddy)
        if len(runtime.moves) != len(expected_moves):
            errors.append("Patched draft changed the move structure "
                          f"({len(runtime.moves)} runtime moves vs {len(expected_moves)} expected)")
        else:
            for runtime_move, expected_move in zip(runtime.moves, expected_moves):
                if runtime_move.motion != expected_move["motion"]:
                    errors.append(f"Move {expected_move['id']} changed motion at runtime "
                                  f"({runtime_move.motion} vs {expected_move['motion']})")
                    break
                if expected_move["end"] is None:
                    continue  # machine-coordinate move: frame is unknown by design
                for axis in ("x", "y", "z"):
                    expected_value = expected_move["end"][axis]
                    runtime_value = (getattr(runtime_move.end, axis, None)
                                     if runtime_move.end is not None else None)
                    if expected_value is None or runtime_value is None:
                        if (expected_value is None) != (runtime_value is None):
                            errors.append(
                                f"Move {expected_move['id']}: {axis} resolution changed at runtime")
                            break
                    elif abs(runtime_value - expected_value) > tolerance:
                        errors.append(
                            f"Move {expected_move['id']}: runtime {axis} {runtime_value:.4f} does "
                            f"not match the expected {expected_value:.4f}")
                        break
                else:
                    runtime_center = (runtime_move.arc.center if runtime_move.arc is not None else None) or {}
                    expected_center = expected_move["center"] or {}
                    for axis in ("x", "y"):
                        expected_axis = expected_center.get(axis)
                        runtime_axis = runtime_center.get(axis)
                        if expected_axis is None or runtime_axis is None:
                            if (expected_axis is None) != (runtime_axis is None):
                                errors.append(f"Move {expected_move['id']}: arc centre {axis} "
                                              "resolution changed at runtime")
                                break
                        elif abs(runtime_axis - expected_axis) > tolerance:
                            errors.append(
                                f"Move {expected_move['id']}: runtime arc centre {axis} "
                                f"{runtime_axis:.4f} does not match the expected {expected_axis:.4f}")
                            break
                if errors:
                    break
        # order-based block mapping (runtime ids shift after insertions)
        if not errors:
            transformed_orig_blocks = [block for block in transformed.blocks]
            runtime_orig_blocks = []
            runtime_copy_blocks = []
            runtime_index = 0
            for block in transformed_orig_blocks:
                if runtime_index >= len(runtime.blocks):
                    errors.append("Patched draft lost blocks at runtime")
                    break
                block_map[block.id] = runtime.blocks[runtime_index]
                runtime_orig_blocks.append(runtime.blocks[runtime_index])
                runtime_index += 1
                for delta in copy_plan.get(block.id, []):
                    if runtime_index >= len(runtime.blocks):
                        errors.append("Patched draft lost array copies at runtime")
                        break
                    runtime_copy_blocks.append((block, delta, runtime.blocks[runtime_index]))
                    runtime_index += 1
            copy_blocks = runtime_copy_blocks
            if runtime_index != len(runtime.blocks) and not errors:
                errors.append("Patched draft has unaccounted blocks at runtime")

        # copy bounds proof
        orig_feature_by_block = {feature["block_id"]: feature for feature in recognition["features"]}
        for source_block, delta, rt_block in copy_blocks:
            source_feature = orig_feature_by_block.get(source_block.id) or {}
            source_bounds = source_feature.get("bounds")
            runtime_bounds = (runtime_features.get(f"feature-{rt_block.id}") or {}).get("bounds")
            if not source_bounds or not runtime_bounds:
                errors.append(f"Array copy of {source_block.id}: bounds unavailable at runtime")
                continue
            for key in ("x_min", "x_max", "y_min", "y_max"):
                axis_delta = delta[0] if key.startswith("x") else delta[1]
                target = source_bounds[key] + axis_delta
                if abs(runtime_bounds[key] - target) > tolerance:
                    errors.append(
                        f"Array copy of {source_block.id}: runtime {key} {runtime_bounds[key]:.4f} "
                        f"does not match the requested offset ({target:.4f})")

        # Z never changes anywhere in the walk (already covered per move)
    else:
        if len(runtime.moves) != len(program.moves) or len(runtime.blocks) != len(program.blocks):
            errors.append("Patched draft changed the program structure (move/block count)")

    def _runtime_feature_bounds(feature_id: str, feature: dict[str, Any]) -> dict[str, Any] | None:
        if has_copies:
            rt_block = block_map.get(feature["block_id"])
            if rt_block is None:
                return None
            return (runtime_features.get(f"feature-{rt_block.id}") or {}).get("bounds")
        return (runtime_features.get(feature_id) or {}).get("bounds")

    for item in normalized:
        feature_id = item["feature_id"]
        original_bounds = feature_by_id[feature_id]["bounds"]
        runtime_bounds = _runtime_feature_bounds(feature_id, feature_by_id[feature_id])
        if not runtime_bounds:
            errors.append(f"Feature {feature_id}: edited feature is missing from the runtime reparse")
            continue
        if item["op"] == "shift":
            expected = {"x_min": original_bounds["x_min"] + item["dx"],
                        "x_max": original_bounds["x_max"] + item["dx"],
                        "y_min": original_bounds["y_min"] + item["dy"],
                        "y_max": original_bounds["y_max"] + item["dy"]}
        elif item["op"] == "resize_x":
            if item["anchor"] == "min":
                expected = {"x_min": item["band_start"], "x_max": item["band_start"] + item["new_length"],
                            "y_min": original_bounds["y_min"], "y_max": original_bounds["y_max"]}
            elif item["anchor"] == "max":
                expected = {"x_min": item["band_end"] - item["new_length"], "x_max": item["band_end"],
                            "y_min": original_bounds["y_min"], "y_max": original_bounds["y_max"]}
            else:
                middle = (item["band_start"] + item["band_end"]) / 2
                expected = {"x_min": middle - item["new_length"] / 2,
                            "x_max": middle + item["new_length"] / 2,
                            "y_min": original_bounds["y_min"], "y_max": original_bounds["y_max"]}
        elif item["op"] == "rotate":
            cx, cy = item["center_x"], item["center_y"]
            x_min, x_max = original_bounds["x_min"], original_bounds["x_max"]
            y_min, y_max = original_bounds["y_min"], original_bounds["y_max"]
            if item["dir"] == "ccw":
                expected = {"x_min": cx - (y_max - cy), "x_max": cx - (y_min - cy),
                            "y_min": cy + (x_min - cx), "y_max": cy + (x_max - cx)}
            else:
                expected = {"x_min": cx + (y_min - cy), "x_max": cx + (y_max - cy),
                            "y_min": cy - (x_max - cx), "y_max": cy - (x_min - cx)}
        else:  # mirror / array: the feature's own bounds are preserved
            expected = {key: original_bounds[key] for key in ("x_min", "x_max", "y_min", "y_max")}
        for key, target in expected.items():
            if abs(runtime_bounds[key] - target) > tolerance:
                errors.append(
                    f"Feature {feature_id}: runtime {key} {runtime_bounds[key]:.4f} does not match "
                    f"the requested edit ({target:.4f})")
    for feature_id, feature in feature_by_id.items():
        if feature_id in edit_by_feature or not feature.get("bounds"):
            continue
        runtime_bounds = _runtime_feature_bounds(feature_id, feature)
        if not runtime_bounds:
            errors.append(f"Feature {feature_id}: untouched feature is missing from the runtime reparse")
            continue
        for key in ("x_min", "x_max", "y_min", "y_max"):
            if abs(runtime_bounds[key] - feature["bounds"][key]) > tolerance:
                errors.append(f"Feature {feature_id}: untouched feature moved on {key}")

    # Phase-two collision gate on the runtime reparse: bounds-level, so it is
    # honest about its limits. A partial overlap that the edit CREATES is
    # rejected; a partial overlap that ALREADY exists in the source (grooves
    # crossing a raster fill, paths running to the sheet edge) is the
    # designer's original intent and stays the operator's responsibility —
    # it is counted and reported, not silently ignored. Full containment
    # counts as legitimate nesting (a hole inside a contour); exact touching
    # is allowed; members of one depth-repeat group share the same XY path
    # and never collide with each other by definition.
    edited_ids = {item["feature_id"] for item in normalized}
    collision_flagged: list[dict[str, Any]] = []
    checked_pairs = 0
    preexisting_skipped = 0
    replicated_skipped = 0
    geometry_cleared = 0
    gate_warnings: list[str] = []
    bounds_ids = sorted(fid for fid, item in runtime_features.items() if item.get("bounds"))

    def _contains(outer: dict[str, float], inner: dict[str, float]) -> bool:
        return (outer["x_min"] <= inner["x_min"] + tolerance
                and outer["x_max"] >= inner["x_max"] - tolerance
                and outer["y_min"] <= inner["y_min"] + tolerance
                and outer["y_max"] >= inner["y_max"] - tolerance)

    def _pair_state(a: dict[str, float], b: dict[str, float]) -> str:
        overlap_x = min(a["x_max"], b["x_max"]) - max(a["x_min"], b["x_min"])
        overlap_y = min(a["y_max"], b["y_max"]) - max(a["y_min"], b["y_min"])
        if overlap_x <= tolerance or overlap_y <= tolerance:
            return "disjoint"
        if _contains(a, b) or _contains(b, a):
            return "contained"
        return "partial"

    def _flag(id_a: str, id_b: str) -> None:
        nonlocal geometry_cleared
        overlap_x = min(runtime_features[id_a]["bounds"]["x_max"], runtime_features[id_b]["bounds"]["x_max"]) \
            - max(runtime_features[id_a]["bounds"]["x_min"], runtime_features[id_b]["bounds"]["x_min"])
        overlap_y = min(runtime_features[id_a]["bounds"]["y_max"], runtime_features[id_b]["bounds"]["y_max"]) \
            - max(runtime_features[id_a]["bounds"]["y_min"], runtime_features[id_b]["bounds"]["y_min"])
        entry: dict[str, Any] = {"features": [id_a, id_b],
                                 "overlap_x": round(overlap_x, 6), "overlap_y": round(overlap_y, 6)}
        # Geometry-verified refinement: bbox partial overlap with clear cutting
        # paths is a false alarm on nested sheets — downgrade to a non-blocking
        # warning (the map UI still draws it). Crossing paths stay a hard error.
        min_dist: float | None = None
        segs_a = _feature_cut_segments(runtime, id_a[len("feature-"):])
        segs_b = _feature_cut_segments(runtime, id_b[len("feature-"):])
        if segs_a and segs_b and len(segs_a) * len(segs_b) <= _GEOM_PAIR_BUDGET:
            if _segments_intersect_any(segs_a, segs_b):
                entry["path_intersection"] = True
            else:
                min_dist = _segments_min_distance(segs_a, segs_b)
                entry["path_min_distance"] = round(min_dist, 4)
        if entry.get("path_intersection") is not True and min_dist is not None:
            entry["severity"] = "warning"
            geometry_cleared += 1
            collision_flagged.append(entry)
            gate_warnings.append(
                f"Features {id_a} and {id_b} overlap only at bbox level ({overlap_x:.3f} x "
                f"{overlap_y:.3f} mm) but their cutting paths stay {min_dist:.3f} mm apart — "
                "no path collision; check the finished clearance (tool diameter) yourself")
            return
        entry["severity"] = "error"
        collision_flagged.append(entry)
        errors.append(
            f"Features {id_a} and {id_b} newly partially overlap after the edits "
            f"({overlap_x:.3f} x {overlap_y:.3f} mm at bounds level); move or shrink one of them")

    if has_copies:
        # Generalized gate for array copies: each runtime feature maps back to
        # a source feature (provenance). A copy that partially overlaps its own
        # source or a sibling copy means the pitch is smaller than the feature;
        # a partial overlap whose relative arrangement differs from the source
        # arrangement is new and rejected. A pair of features that moved by the
        # SAME delta keeps the source's own relative arrangement and is
        # skipped as replicated; original-only pairs keep the phase-two rule.
        provenance: dict[str, str] = {}
        is_copy: set[str] = set()
        pure_translation: dict[str, bool] = {}
        resized_block_ids = {item["block_id"] for item in normalized
                             if item["op"] in ("resize_x", "rotate")}
        for block in transformed.blocks:
            rt_block = block_map.get(block.id)
            if rt_block is None:
                continue
            rt_fid = f"feature-{rt_block.id}"
            provenance[rt_fid] = f"feature-{block.id}"
            pure_translation[rt_fid] = block.id not in resized_block_ids
        for source_block, delta, rt_block in copy_blocks:
            rt_fid = f"feature-{rt_block.id}"
            provenance[rt_fid] = f"feature-{source_block.id}"
            is_copy.add(rt_fid)
            pure_translation[rt_fid] = True
    if has_copies and not errors:
        for index_a in range(len(bounds_ids)):
            for index_b in range(index_a + 1, len(bounds_ids)):
                id_a, id_b = bounds_ids[index_a], bounds_ids[index_b]
                provenance_a = provenance.get(id_a, id_a)
                provenance_b = provenance.get(id_b, id_b)
                if provenance_a not in feature_by_id or provenance_b not in feature_by_id:
                    continue  # structure errors already reported above
                copy_involved = id_a in is_copy or id_b in is_copy
                if not copy_involved and provenance_a not in edited_ids and provenance_b not in edited_ids:
                    continue
                if provenance_b in group_of.get(provenance_a, ()):
                    continue
                if _pair_state(runtime_features[id_a]["bounds"], runtime_features[id_b]["bounds"]) != "partial":
                    continue
                if copy_involved:
                    checked_pairs += 1
                    if provenance_a == provenance_b:
                        _flag(id_a, id_b)
                        continue
                    if pure_translation.get(id_a) and pure_translation.get(id_b):
                        source_a = feature_by_id[provenance_a]["bounds"]
                        source_b = feature_by_id[provenance_b]["bounds"]
                        relative_runtime = {key: runtime_features[id_a]["bounds"][key]
                                            - runtime_features[id_b]["bounds"][key]
                                            for key in ("x_min", "y_min")}
                        relative_source = {key: source_a[key] - source_b[key]
                                           for key in ("x_min", "y_min")}
                        if all(abs(relative_runtime[key] - relative_source[key]) <= tolerance
                               for key in relative_runtime):
                            replicated_skipped += 1
                            continue
                    _flag(id_a, id_b)
                else:
                    if _pair_state(feature_by_id[provenance_a]["bounds"],
                                   feature_by_id[provenance_b]["bounds"]) == "partial":
                        preexisting_skipped += 1
                        continue
                    checked_pairs += 1
                    _flag(id_a, id_b)
    else:
        for index_a in range(len(bounds_ids)):
            for index_b in range(index_a + 1, len(bounds_ids)):
                id_a, id_b = bounds_ids[index_a], bounds_ids[index_b]
                if id_a not in edited_ids and id_b not in edited_ids:
                    continue  # untouched pairs already coexist in the source
                if id_b in group_of.get(id_a, ()):  # same XY path at multiple depths
                    continue
                if _pair_state(feature_by_id[id_a]["bounds"], feature_by_id[id_b]["bounds"]) == "partial":
                    preexisting_skipped += 1
                    continue  # the source already lets these coexist
                checked_pairs += 1
                state = _pair_state(runtime_features[id_a]["bounds"], runtime_features[id_b]["bounds"])
                if state != "partial":
                    continue  # disjoint, touching or nested at runtime
                _flag(id_a, id_b)

    collision_check = {"method": "bounds_level_new_overlaps_rejected",
                       "checked_pairs": checked_pairs, "preexisting_skipped": preexisting_skipped,
                       "replicated_skipped": replicated_skipped, "geometry_cleared": geometry_cleared,
                       "flagged": collision_flagged}

    if not has_copies and not has_mirror and not has_rotated:
        for before, after in zip(program.moves, runtime.moves):
            z_before = before.end.z if before.end is not None else None
            z_after = after.end.z if after.end is not None else None
            if z_before != z_after:
                errors.append(f"Move {before.id} changed Z at runtime")
                break

    collision_warning = ("Collision check ran at bounds level (new partial overlaps rejected; containment "
                         "counts as nesting; overlaps already present in the source are left to the operator); "
                         "bbox-only overlaps whose runtime cutting paths stay clear are downgraded to a "
                         "warning with the real path distance; thin-wall and material-edge placement still "
                         "needs operator eyes.")
    result_warnings = [collision_warning] + gate_warnings
    if array_summaries:
        result_warnings.append(
            "Array and grid copies are inserted right after their source block (per depth-pass member, "
            "grid rows in Y-major order), carry no N sequence word, and each copy keeps the source "
            "block's own cutting order.")
    if has_mirror:
        result_warnings.append(
            "Mirroring keeps the feature's bounds but reverses every arc direction and the cutting "
            "direction (climb <-> conventional); feed, tool and Z stay untouched.")
    if has_rotated:
        result_warnings.append(
            "Rotating by 90 degrees keeps arc directions (G2/G3 stay, unlike mirroring) and "
            "cutter-compensation sides; every cut line of a rotated block ends up explicit on both "
            "axes (a missing X/Y word is inserted next to the existing words) and the feature's "
            "width and height swap around the same centre.")
    extras = {"array_applied": array_summaries}
    if block_copies:
        extras["block_copies"] = block_copies
    if mirror_flips:
        extras["arc_direction_flips"] = mirror_flips
    if rotated_move_ids:
        # The machine-output path re-serializes from this result dict, so the
        # rotated-move declaration must travel with it (same contract as
        # arc_direction_flips for mirrors) or the word insertions would be
        # silently lost on the way to the dialect gate.
        extras["rotated_move_ids"] = rotated_move_ids
    if errors:
        return {
            "preview_only": True, "safe_for_execution": False,
            "source_sha256": program.source_sha256,
            "operation": "interactive_edits",
            "edits_applied": normalized,
            "constraints_applied": constraints_applied,
            **extras,
            "collision_check": collision_check,
            "original": recognition,
            # Runtime truth (modal chains re-resolved on the patched draft) —
            # what the map preview should draw, not the static stale IR.
            "resized": runtime_recognition,
            "validation": {"passed": False, "errors": errors, "warnings": result_warnings},
            "program_ir": to_dict(transformed),
        }
    return {
        "preview_only": True, "safe_for_execution": False,
        "source_sha256": program.source_sha256,
        "operation": "interactive_edits",
        "edits_applied": normalized,
        "constraints_applied": constraints_applied,
        **extras,
        "collision_check": collision_check,
        "original": recognition,
        "resized": runtime_recognition,
        "validation": {"passed": True, "errors": [], "warnings": result_warnings},
        "program_ir": to_dict(transformed),
    }
