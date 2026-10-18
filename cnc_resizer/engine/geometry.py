"""Geometry reconstruction from the CNC intermediate representation."""
from __future__ import annotations

import math
from typing import Any

from .ir import PathBlock, Point3, Program


def _point_dict(point: Point3 | None) -> dict[str, float | None] | None:
    if point is None:
        return None
    return {"x": point.x, "y": point.y, "z": point.z}


def _angle_in_sweep(angle: float, start: float, sweep: float, tolerance: float = 1e-9) -> bool:
    tau = 2 * math.pi
    if sweep >= 0:
        return ((angle - start) % tau) <= sweep + tolerance
    return ((start - angle) % tau) <= -sweep + tolerance


def block_geometry(block: PathBlock) -> dict[str, Any]:
    cuts = [move for move in block.motions if move.motion in ("linear", "arc_cw", "arc_ccw")]
    points: list[dict[str, float]] = []
    unresolved_arcs = []
    z_values: list[float] = []
    connectivity_errors = []
    last_end: Point3 | None = None
    arc_count = 0
    for move in cuts:
        if move.start is None or move.end is None:
            unresolved_arcs.append(move.id)
            continue
        if last_end is not None:
            if math.hypot(last_end.x - move.start.x, last_end.y - move.start.y) > 0.05:
                connectivity_errors.append({"before_move": move.id, "gap": round(math.hypot(last_end.x-move.start.x, last_end.y-move.start.y), 6)})
        last_end = move.end
        points.extend([{"x": move.start.x, "y": move.start.y}, {"x": move.end.x, "y": move.end.y}])
        # A cut block commonly starts with the previous rapid's clearance Z;
        # including that start point falsely reports safe height as cut depth.
        if move.end.z is not None:
            z_values.append(move.end.z)
        if move.arc:
            arc_count += 1
            arc = move.arc
            plane_axes = {"XY": ("x", "y"), "XZ": ("x", "z"), "YZ": ("y", "z")}
            if arc.plane in plane_axes and arc.center and arc.radius is not None and arc.sweep_radians is not None and arc.valid:
                first, second = plane_axes[arc.plane]
                start_angle = math.atan2(getattr(move.start, second) - arc.center[second],
                                         getattr(move.start, first) - arc.center[first])
                sweep = arc.sweep_radians
                for angle in (0, math.pi/2, math.pi, 3*math.pi/2):
                    if not _angle_in_sweep(angle, start_angle, sweep):
                        continue
                    fraction = (((angle-start_angle) % (2*math.pi))/sweep if sweep > 0
                                else ((start_angle-angle) % (2*math.pi))/(-sweep))
                    projected = {}
                    for axis in "xyz":
                        if axis == first:
                            projected[axis] = arc.center[first] + arc.radius*math.cos(angle)
                        elif axis == second:
                            projected[axis] = arc.center[second] + arc.radius*math.sin(angle)
                        else:
                            start_value, end_value = getattr(move.start, axis), getattr(move.end, axis)
                            projected[axis] = (start_value + (end_value-start_value)*fraction
                                               if start_value is not None and end_value is not None else None)
                    if projected["x"] is not None and projected["y"] is not None:
                        points.append({"x": projected["x"], "y": projected["y"]})
            else:
                unresolved_arcs.append(move.id)
    if points:
        xs = [point["x"] for point in points]
        ys = [point["y"] for point in points]
        bounds = {"x_min": min(xs), "y_min": min(ys), "x_max": max(xs), "y_max": max(ys),
                  "width": max(xs)-min(xs), "height": max(ys)-min(ys),
                  "quality": ("endpoint_bounds_only" if unresolved_arcs else
                              "assumed_g17_projected_arc_bounds" if any(move.arc and move.arc.plane_assumed for move in cuts) else
                              "exact_projected_arc_bounds") if arc_count else "linear_endpoints"}
    else:
        bounds = None
    start_point = next((move.start for move in cuts if move.start is not None), None)
    end_point = next((move.end for move in reversed(cuts) if move.end is not None), None)
    closed = bool(start_point and end_point and math.hypot(start_point.x-end_point.x, start_point.y-end_point.y) <= 0.05)
    return {
        "block_id": block.id,
        "source_line_start": block.started_by_line,
        "source_line_end": block.ended_at_line,
        "start": _point_dict(start_point or block.start),
        "end": _point_dict(end_point),
        "closed": closed,
        "bounds": bounds,
        "depth_min": min(z_values) if z_values else None,
        "depth_max": max(z_values) if z_values else None,
        "depth_values": sorted(set(z_values)),
        "tools": sorted({move.tool for move in cuts if move.tool is not None}),
        "move_ids": [move.id for move in cuts],
        "move_count": len(cuts),
        "linear_count": sum(move.motion == "linear" for move in cuts),
        "arc_count": arc_count,
        "unresolved_moves": sorted(set(unresolved_arcs)),
        "connectivity_errors": connectivity_errors,
        "geometry_points": points,
    }


def program_geometry(program: Program) -> dict[str, Any]:
    blocks = [block_geometry(block) for block in program.blocks]
    geometries = [block for block in blocks if block["bounds"]]
    if geometries:
        bounds = {
            "x_min": min(block["bounds"]["x_min"] for block in geometries),
            "y_min": min(block["bounds"]["y_min"] for block in geometries),
            "x_max": max(block["bounds"]["x_max"] for block in geometries),
            "y_max": max(block["bounds"]["y_max"] for block in geometries),
        }
        bounds["width"] = bounds["x_max"] - bounds["x_min"]
        bounds["height"] = bounds["y_max"] - bounds["y_min"]
    else:
        bounds = None
    return {"bounds": bounds, "blocks": blocks}
