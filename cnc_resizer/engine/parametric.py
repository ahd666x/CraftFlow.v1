"""Constraint-based preview transforms. This module never serializes G-code."""
from __future__ import annotations

import copy
import math
from typing import Any

from .geometry import program_geometry
from .ir import Program, to_dict
from .parser import parse_program
from .recognition import recognize
from .serializer import SerializerBlocked, serialize_candidate
from .validation import validate_equivalence


class TransformError(ValueError):
    pass


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
    move's END are what the source lines can patch. Z-only plunges/retracts
    inside the block carry their XY modally at runtime (their lines have no
    X/Y words), so they are intentionally left untouched in the candidate IR.
    Cut moves shift start+end together and arc centres translate with them,
    which keeps every I/J offset identical.
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
        raw_words = move.raw_words or {}
        if move.end is None or move.end.x is None or move.end.y is None:
            raise TransformError(f"Feature {feature_id}: move {move.id} has unresolved coordinates")
        # Per-axis word rule: an axis is patched only when its word exists on
        # the line. A line without the word (e.g. "G1Y190.000" with modal X)
        # carries that axis modally at runtime — the patched chain around it
        # moves the modal position — so its static IR value must stay stale
        # and the source line byte-identical.
        if "x" in raw_words:
            move.end.x += dx
        if "y" in raw_words:
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


def preview_edits(program: Program, request: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Interactive rigid per-feature edits (phase one/two: shift, X stretch,
    declarative placement constraints).

    A ``shift`` edit translates one recognized feature's whole block by
    ``(dx, dy)``; a ``resize_x`` edit stretches one feature's own X span to
    ``new_length`` around an anchor (``min``/``max``/``center``). Phase two
    adds three constraint ops that are resolved against the ORIGINAL geometry
    into derived rigid shifts: ``center_on`` (feature or program-envelope
    centre), ``set_gap`` (exact edge-to-edge gap to an anchor feature on one
    axis) and ``center_between`` (equidistant midpoint of two anchor
    features). A constraint on a member of a depth-repeat group broadcasts the
    identical derived shift to every member. The candidate is accepted only
    after the pure-coordinate-patch serializer accepts it AND a runtime
    reparse of the patched draft proves every edited feature reached exactly
    its requested geometry while every untouched feature stayed put, no Z
    value changed, and no edit CREATED a new partial bounds overlap between
    two features (overlaps already present in the source stay the operator's
    responsibility; full containment counts as legitimate nesting). Features
    sharing the same XY path at multiple depths must be edited together with
    identical parameters or not at all.
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
        if not isinstance(edit, dict) or edit.get("op") not in ("shift", "resize_x",
                                                                "center_on", "set_gap", "center_between"):
            raise TransformError(
                f"{label}: op must be one of shift, resize_x, center_on, set_gap, center_between")
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

    # ---- phase two: resolve constraints into derived rigid shifts ----
    constraints_applied: list[dict[str, Any]] = []
    group_of: dict[str, list[str]] = {}
    for pattern in recognition["patterns"]:
        if pattern["kind"] == "same_xy_path_at_multiple_depths":
            ids = list(pattern["feature_ids"])
            for fid in ids:
                group_of[fid] = ids
    taken: set[str] = {item["feature_id"] for item in normalized}
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
                            if key not in ("feature_id", "block_id")))

    for pattern in recognition["patterns"]:
        if pattern["kind"] != "same_xy_path_at_multiple_depths":
            continue
        group = list(pattern["feature_ids"])
        touched = [fid for fid in group if fid in edit_by_feature]
        if not touched:
            continue
        if len(touched) < len(group):
            raise TransformError(
                "Features " + ", ".join(sorted(group)) + " share the same XY path at multiple depths; "
                "apply the identical edit to all of them or none of them")
        reference = edit_signature(edit_by_feature[touched[0]])
        if any(edit_signature(edit_by_feature[fid]) != reference for fid in touched):
            detail = ("identical shift delta" if reference and dict(reference).get("op") == "shift"
                      else "identical resize_x parameters")
            raise TransformError(
                "Features sharing the same XY path at multiple depths must receive the " + detail)

    transformed = copy.deepcopy(program)
    blocks = {block.id: block for block in transformed.blocks}
    for item in normalized:
        block = blocks.get(item["block_id"])
        if block is None:
            raise TransformError(f"Feature {item['feature_id']}: source block vanished")
        if item["op"] == "shift":
            _shift_block_rigid(block, item["dx"], item["dy"], item["feature_id"])
        else:
            delta = item["new_length"] - (item["band_end"] - item["band_start"])
            below, above = {"min": (0.0, delta), "max": (-delta, 0.0),
                            "center": (-delta / 2.0, delta / 2.0)}[item["anchor"]]
            _stretch_feature_x(block, item["feature_id"], item["band_start"], item["band_end"],
                               below, above)

    provisional = {"validation": {"passed": True, "errors": [], "warnings": []},
                   "source_sha256": program.source_sha256,
                   "program_ir": to_dict(transformed)}
    try:
        draft = serialize_candidate(source_text, provisional)
    except SerializerBlocked as exc:
        blockers = getattr(exc, "blockers", None) or [str(exc)]
        raise TransformError(
            "Interactive edit cannot be expressed as a safe coordinate patch: " + "؛ ".join(blockers)) from exc

    tolerance = 1e-6
    errors: list[str] = []
    runtime = parse_program(draft["text"])
    runtime_recognition = recognize(runtime)
    runtime_features = {feature["id"]: feature for feature in runtime_recognition["features"]}
    if len(runtime.moves) != len(program.moves) or len(runtime.blocks) != len(program.blocks):
        errors.append("Patched draft changed the program structure (move/block count)")
    for item in normalized:
        feature_id = item["feature_id"]
        original_bounds = feature_by_id[feature_id]["bounds"]
        runtime_bounds = (runtime_features.get(feature_id) or {}).get("bounds")
        if not runtime_bounds:
            errors.append(f"Feature {feature_id}: edited feature is missing from the runtime reparse")
            continue
        if item["op"] == "shift":
            expected = {"x_min": original_bounds["x_min"] + item["dx"],
                        "x_max": original_bounds["x_max"] + item["dx"],
                        "y_min": original_bounds["y_min"] + item["dy"],
                        "y_max": original_bounds["y_max"] + item["dy"]}
        elif item["anchor"] == "min":
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
        for key, target in expected.items():
            if abs(runtime_bounds[key] - target) > tolerance:
                errors.append(
                    f"Feature {feature_id}: runtime {key} {runtime_bounds[key]:.4f} does not match "
                    f"the requested edit ({target:.4f})")
    for feature_id, feature in feature_by_id.items():
        if feature_id in edit_by_feature or not feature.get("bounds"):
            continue
        runtime_bounds = (runtime_features.get(feature_id) or {}).get("bounds")
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
            overlap_x = min(runtime_features[id_a]["bounds"]["x_max"], runtime_features[id_b]["bounds"]["x_max"]) \
                - max(runtime_features[id_a]["bounds"]["x_min"], runtime_features[id_b]["bounds"]["x_min"])
            overlap_y = min(runtime_features[id_a]["bounds"]["y_max"], runtime_features[id_b]["bounds"]["y_max"]) \
                - max(runtime_features[id_a]["bounds"]["y_min"], runtime_features[id_b]["bounds"]["y_min"])
            collision_flagged.append({"features": [id_a, id_b],
                                      "overlap_x": round(overlap_x, 6), "overlap_y": round(overlap_y, 6)})
            errors.append(
                f"Features {id_a} and {id_b} newly partially overlap after the edits "
                f"({overlap_x:.3f} x {overlap_y:.3f} mm at bounds level); move or shrink one of them")

    collision_check = {"method": "bounds_level_new_overlaps_rejected",
                       "checked_pairs": checked_pairs, "preexisting_skipped": preexisting_skipped,
                       "flagged": collision_flagged}

    for before, after in zip(program.moves, runtime.moves):
        z_before = before.end.z if before.end is not None else None
        z_after = after.end.z if after.end is not None else None
        if z_before != z_after:
            errors.append(f"Move {before.id} changed Z at runtime")
            break

    collision_warning = ("Collision check ran at bounds level (new partial overlaps rejected; containment "
                         "counts as nesting; overlaps already present in the source are left to the operator); "
                         "thin-wall and material-edge placement still needs operator eyes.")
    if errors:
        return {
            "preview_only": True, "safe_for_execution": False,
            "source_sha256": program.source_sha256,
            "operation": "interactive_edits",
            "edits_applied": normalized,
            "constraints_applied": constraints_applied,
            "collision_check": collision_check,
            "original": recognition,
            # Runtime truth (modal chains re-resolved on the patched draft) —
            # what the map preview should draw, not the static stale IR.
            "resized": runtime_recognition,
            "validation": {"passed": False, "errors": errors, "warnings": [collision_warning]},
            "program_ir": to_dict(transformed),
        }
    return {
        "preview_only": True, "safe_for_execution": False,
        "source_sha256": program.source_sha256,
        "operation": "interactive_edits",
        "edits_applied": normalized,
        "constraints_applied": constraints_applied,
        "collision_check": collision_check,
        "original": recognition,
        "resized": runtime_recognition,
        "validation": {"passed": True, "errors": [], "warnings": [collision_warning]},
        "program_ir": to_dict(transformed),
    }
