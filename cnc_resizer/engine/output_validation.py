"""Fail-closed preflight for transformed CNC candidates; never serializes machine code."""
from __future__ import annotations

import math
from typing import Any, Callable

from .postprocessor import execution_gate
from .serializer import SerializerBlocked, serialize_candidate
from .validation import validate_program


def _finite_limit(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"machine_limits.{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"machine_limits.{name} must be finite")
    return result


def validate_output_candidate(source: Any, preview: dict[str, Any],
                              machine_limits: dict[str, Any] | None = None, *,
                              source_text: str | None = None,
                              dialect_gate: Callable[[str, str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Report output blockers and an auditable geometry diff.

    Without a dialect gate the report stays fail-closed exactly as before:
    ``allowed`` is always false because no controller adapter is consulted.
    When ``dialect_gate`` (the verified ``minimal_xy_router`` postprocessor)
    and the ``source_text`` are supplied, the candidate may be allowed: the
    engine-side source-frame blockers (undeclared units, G54/G43 semantics,
    unknown codes) are then superseded by the gate's stronger line-by-line
    re-verification of the FINAL patched text, while geometry, traceability
    and machine-travel blockers still apply.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    limits = machine_limits or {}
    if not isinstance(limits, dict):
        raise ValueError("machine_limits must be an object")
    bridge_active = callable(dialect_gate) and isinstance(source_text, str) and bool(source_text.strip())
    source_gate = execution_gate(source)
    if bridge_active:
        warnings.extend(f"dialect postprocessor re-verifies the final text: {reason}"
                        for reason in source_gate["reasons"])
    else:
        blockers.extend(source_gate["reasons"])
        if source.units is None:
            blockers.append("Source units are undeclared; physical machine limits cannot be verified")
        if source.coordinate_system:
            blockers.append("Work-coordinate offset is unresolved; source coordinates cannot be checked against machine travel")
        if any(code == "G43" or code.startswith("G43.") for code in source.observed_g_codes):
            blockers.append("Tool-length compensation is unresolved; physical Z travel cannot be verified")
    source_validation = validate_program(source)
    if not source_validation["geometry_valid_for_preview"]:
        blockers.extend(source_validation["errors"])
    candidate_dict = preview.get("program_ir")
    if not isinstance(candidate_dict, dict):
        blockers.append("Transformed intermediate representation is missing")
        candidate_moves = []
        candidate_geometry = None
    else:
        candidate_moves = candidate_dict.get("moves", [])
        candidate_geometry = preview.get("resized")
    if not preview.get("validation", {}).get("passed"):
        blockers.extend(preview.get("validation", {}).get("errors", ["Geometry preview validation failed"]))
    if not candidate_geometry or not candidate_geometry.get("bounds"):
        blockers.append("Candidate geometry bounds are unresolved")
    if not limits:
        blockers.append("Machine travel/envelope limits have not been configured")
    else:
        allowed_fields = ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max", "max_feed", "max_spindle")
        unknown_limits = set(limits) - set(allowed_fields)
        if unknown_limits:
            raise ValueError("Unsupported machine limit fields: " + ", ".join(sorted(unknown_limits)))
        normalized: dict[str, float] = {}
        for name in allowed_fields:
            if name in limits:
                normalized[name] = _finite_limit(limits[name], name)
        for axis in "xy":
            if f"{axis}_min" not in normalized or f"{axis}_max" not in normalized:
                blockers.append(f"Machine {axis.upper()} travel min/max limits are incomplete")
            elif normalized[f"{axis}_min"] >= normalized[f"{axis}_max"]:
                blockers.append(f"Machine {axis.upper()} travel limits are inverted")
        if ("z_min" in normalized) != ("z_max" in normalized):
            blockers.append("Machine Z travel requires both min and max limits")
        if "z_min" in normalized and "z_max" in normalized and normalized["z_min"] >= normalized["z_max"]:
            blockers.append("Machine Z travel limits are inverted")
        for field in ("max_feed", "max_spindle"):
            if field in normalized and normalized[field] <= 0:
                blockers.append(f"Machine {field} must be greater than zero")
        if candidate_geometry and candidate_geometry.get("bounds"):
            bounds = candidate_geometry["bounds"]
            candidate_xy: dict[str, list[float]] = {"x": [], "y": []}
            for move in (candidate_dict or {}).get("moves", []):
                if move.get("machine_coordinates"):
                    continue
                for point_name in ("start", "end"):
                    point = move.get(point_name) or {}
                    for axis in "xy":
                        if point.get(axis) is not None:
                            candidate_xy[axis].append(float(point[axis]))
            for axis in "xy":
                lower = min([bounds[f"{axis}_min"], *candidate_xy[axis]]) if candidate_xy[axis] else bounds[f"{axis}_min"]
                upper = max([bounds[f"{axis}_max"], *candidate_xy[axis]]) if candidate_xy[axis] else bounds[f"{axis}_max"]
                if f"{axis}_min" in normalized and lower < normalized[f"{axis}_min"]:
                    blockers.append(f"Candidate {axis.upper()} minimum exceeds configured machine travel")
                if f"{axis}_max" in normalized and upper > normalized[f"{axis}_max"]:
                    blockers.append(f"Candidate {axis.upper()} maximum exceeds configured machine travel")
            if "z_min" in normalized or "z_max" in normalized:
                z_values = [point["z"] for move in (candidate_dict or {}).get("moves", [])
                            for point in (move.get("start"), move.get("end"))
                            if point and point.get("z") is not None]
                if "z_min" in normalized and z_values and min(z_values) < normalized["z_min"]:
                    blockers.append("Candidate Z minimum exceeds configured machine travel")
                if "z_max" in normalized and z_values and max(z_values) > normalized["z_max"]:
                    blockers.append("Candidate Z maximum exceeds configured machine travel")
        if "max_feed" in normalized and source.feed_values:
            feed_scale = 25.4 if source.units == "inch" else 1.0
            if max(source.feed_values) * feed_scale > normalized["max_feed"]:
                blockers.append("Source feed exceeds configured maximum feed")
        if "max_spindle" in normalized and source.spindle_values and max(source.spindle_values) > normalized["max_spindle"]:
            blockers.append("Source spindle speed exceeds configured maximum spindle speed")

    if source.coordinate_system:
        warnings.append("Work-coordinate offset is not resolved to a physical machine origin")
    if any(code == "G43" or code.startswith("G43.") for code in source.observed_g_codes):
        warnings.append("Tool-length compensation is not included in reconstructed machine coordinates")
    original_moves = {move.id: move for move in source.moves}
    changed_ids = []
    lost_moves = []
    motion_semantics_changed = []
    for item in candidate_moves:
        move_id = item.get("id")
        original = original_moves.get(move_id)
        if original is None:
            lost_moves.append(move_id)
            continue
        for point_name in ("start", "end"):
            point = item.get(point_name) or {}
            before = getattr(original, point_name)
            for axis in "xyz":
                after_value = point.get(axis)
                before_value = getattr(before, axis) if before is not None else None
                if before_value is not None and after_value is not None and abs(before_value - after_value) > 1e-8:
                    changed_ids.append(move_id)
        if item.get("motion") != original.motion or item.get("feed") != original.feed or item.get("spindle") != original.spindle or item.get("tool") != original.tool:
            motion_semantics_changed.append(move_id)
    if lost_moves or len(candidate_moves) != len(source.moves):
        blockers.append("Candidate move count/source traceability does not match source")
    if motion_semantics_changed:
        blockers.append("Motion/feed/spindle/tool semantics changed unexpectedly")
    if any(move.arc and move.arc.valid is not True for move in source.moves):
        blockers.append("Source contains unresolved or invalid arcs")
    physical_frame_resolved = bool(
        source.units is not None and not source.coordinate_system
        and not any(code == "G43" or code.startswith("G43.") for code in source.observed_g_codes)
    )
    limits_complete = all(key in limits for key in ("x_min", "x_max", "y_min", "y_max"))
    bridge_report: dict[str, Any] = {"active": False}
    candidate_sha256 = None
    if bridge_active:
        try:
            serialized = serialize_candidate(source_text, preview)
            postprocessed = dialect_gate(source_text, serialized["text"])
        except (SerializerBlocked, ValueError) as exc:
            blockers.append("Machine output via the dialect postprocessor is blocked: " + str(exc))
        else:
            bridge_report = {"active": True, "adapter_key": postprocessed.get("adapter_key"),
                             "units_injected": postprocessed.get("units_injected"),
                             "tool": postprocessed.get("tool"), "h": postprocessed.get("h"),
                             "changed_move_count": serialized["changed_move_count"],
                             "structure_verified": True}
            candidate_sha256 = serialized["candidate_sha256"]
    else:
        blockers.append("No verified controller-specific output serializer or reference simulator is registered")
    return {
        "stage": "7-preflight",
        "allowed": not blockers,
        "output_generated": False,
        "execution_gate": "passed" if not blockers else "blocked",
        "machine_output_bridge": bridge_report,
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": list(dict.fromkeys(warnings)),
        "source_sha256": source.source_sha256,
        "candidate_sha256": candidate_sha256,
        "geometry_before": source_validation.get("bounds"),
        "geometry_after": candidate_geometry.get("bounds") if candidate_geometry else None,
        "changed_move_ids": sorted(set(changed_ids)),
        "changed_move_count": len(set(changed_ids)),
        "lost_move_ids": lost_moves,
        "motion_semantics_changed_move_ids": motion_semantics_changed,
        "machine_limits_checked": bool(limits_complete and physical_frame_resolved),
        "controller_simulation": "workshop_dialect_postprocessor" if bridge_report["active"] else "not_configured",
    }
