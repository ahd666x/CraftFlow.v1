"""Geometry and preview validation; executable readiness is fail-closed."""
from __future__ import annotations

import math
from typing import Any

from .geometry import program_geometry
from .ir import Program


def validate_program(program: Program) -> dict[str, Any]:
    geometry = program_geometry(program)
    errors = []
    warnings = list(program.warnings)
    if not geometry["bounds"]:
        errors.append("No usable XY geometry could be reconstructed")
    unresolved = []
    disconnected = []
    for block in geometry["blocks"]:
        unresolved.extend(block["unresolved_moves"])
        disconnected.extend(block["connectivity_errors"])
    if disconnected:
        errors.append(f"{len(disconnected)} discontinuity/discontinuities found between adjacent motion segments")
    if unresolved:
        warnings.append(f"{len(set(unresolved))} moves have unresolved coordinates or arc geometry")
    if program.units is None:
        warnings.append("Physical dimensions cannot be labeled mm/in because units are not declared")
    if program.unknown_g_codes or program.unknown_m_codes or program.compact_controller_tokens:
        warnings.append("Unknown controller semantics prevent executable output")
    return {
        "geometry_valid_for_preview": not errors and bool(geometry["bounds"]),
        "errors": errors,
        "warnings": sorted(set(warnings)),
        "requires_operator_confirmation": True,
        "controller_approved": False,
        "safe_for_execution": False,
        "machine_output_written": False,
        "bounds": geometry["bounds"],
        "unresolved_move_ids": sorted(set(unresolved)),
        "connectivity_errors": disconnected,
    }


def validate_equivalence(original: Program, transformed: Program, feature_modes: dict[str, str],
                         feature_to_block: dict[str, str]) -> dict[str, Any]:
    """Check fixed-feature coordinates and arc-radius invariants after preview."""
    errors = []
    tolerance = 1e-6
    old_blocks = {block.id: block for block in original.blocks}
    new_blocks = {block.id: block for block in transformed.blocks}
    for feature_id, mode in feature_modes.items():
        block_id = feature_to_block.get(feature_id)
        if block_id is None:
            errors.append(f"Feature {feature_id} has no source block")
            continue
        old_block, new_block = old_blocks.get(block_id), new_blocks.get(block_id)
        if old_block is None or new_block is None:
            errors.append(f"Feature {feature_id} lost its source block")
            continue
        new_moves = {move.id: move for move in new_block.motions}
        for before in old_block.motions:
            after = new_moves.get(before.id)
            if after is None:
                errors.append(f"Motion {before.id} disappeared from preview")
                continue
            if mode == "fixed":
                for point_name in ("start", "end"):
                    point_before, point_after = getattr(before, point_name), getattr(after, point_name)
                    if point_before is None or point_after is None:
                        if point_before != point_after:
                            errors.append(f"Fixed feature {feature_id} changed coordinate resolution on {before.id}")
                        continue
                    for axis in ("x", "y", "z"):
                        value_before, value_after = getattr(point_before, axis), getattr(point_after, axis)
                        if value_before is not None and value_after is not None and abs(value_before-value_after) > tolerance:
                            errors.append(f"Fixed feature {feature_id} moved at {before.id}.{point_name}.{axis}")
            if before.arc and after.arc:
                if before.arc.radius is not None and after.arc.radius is not None and abs(before.arc.radius-after.arc.radius) > tolerance:
                    errors.append(f"Arc radius changed for {before.id}")
                if before.arc.valid is True and after.arc.valid is not True:
                    errors.append(f"Arc validity was lost for {before.id}")
    for move in transformed.moves:
        for point in (move.start, move.end):
            if point:
                for axis in ("x", "y", "z"):
                    value = getattr(point, axis)
                    if value is not None and not math.isfinite(value):
                        errors.append(f"Motion {move.id} contains a non-finite {axis} coordinate")
        if move.arc and move.arc.valid is False:
            errors.append(f"Arc {move.id} has inconsistent radii")
    errors = list(dict.fromkeys(errors))
    return {"passed": not errors, "errors": errors, "safe_for_execution": False}
