"""Geometry-only recognition; labels are candidates, never physical-operation claims."""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from .geometry import program_geometry
from .ir import Program


def _classify(block_geometry: dict[str, Any], moves: list[Any]) -> tuple[str, str, list[str]]:
    bounds = block_geometry["bounds"]
    if not bounds:
        return "unresolved", "none", ["No resolved XY positions"]
    width, height = bounds["width"], bounds["height"]
    if not any(move.motion in ("linear", "arc_cw", "arc_ccw") for move in moves):
        return "rapid_only", "high_motion_type_only", ["No interpolation moves"]
    closed = block_geometry["closed"]
    arcs = [move for move in moves if move.motion in ("arc_cw", "arc_ccw")]
    if closed and arcs and width <= 5 and height <= 5:
        return "small_loop_candidate", "high_geometry_only", ["Closed endpoints", "Arc segments present", "Small XY bounds"]
    if closed and arcs and width > 100 and height > 100:
        return "large_closed_path_candidate", "medium_geometry_only", [
            "Closed endpoints", "Arc segments present", "Large XY bounds", "Not proof of the finished-part boundary",
        ]
    long_pass_directions = []
    for move in moves:
        if move.motion != "linear" or move.start is None or move.end is None:
            continue
        dx, dy = move.end.x - move.start.x, move.end.y - move.start.y
        if abs(dx) >= 50 and abs(dy) <= max(15, abs(dx)*0.03):
            long_pass_directions.append(1 if dx > 0 else -1)
    alternations = sum(a != b for a, b in zip(long_pass_directions, long_pass_directions[1:]))
    ratio = alternations / max(1, len(long_pass_directions)-1)
    if width > 100 and len(long_pass_directions) >= 4 and ratio >= 0.6:
        return "raster_or_zigzag_candidate", "medium_geometric_pattern", [
            f"{len(long_pass_directions)} long near-horizontal passes", f"direction alternation ratio {ratio:.2f}",
        ]
    if closed and width > 100 and height > 100:
        return "large_closed_path_candidate", "medium_geometry_only", [
            "Closed endpoints", "Large XY bounds", "Not proof of the finished-part boundary",
        ]
    if max(width, height) > 100 and min(width, height) <= 20:
        return "elongated_path_candidate", "low_geometric_pattern", [
            "Long, narrow endpoint/arc bounds", "Groove, edge or other operation remains unconfirmed",
        ]
    if width > 100 and height > 100:
        return "large_area_or_open_path_candidate", "low_geometric_pattern", [
            "Large XY bounds", "Not enough evidence to call an outer contour",
        ]
    return "unclassified_path", "low", ["No stage-two geometric rule matched"]


def _raster_analysis(moves: list[Any]) -> dict[str, Any]:
    passes = []
    for move in moves:
        if move.motion != "linear" or move.start is None or move.end is None:
            continue
        dx, dy = move.end.x - move.start.x, move.end.y - move.start.y
        if abs(dx) >= 50 and abs(dy) <= max(15, abs(dx) * 0.03):
            passes.append({"source_line": move.source_line, "y_center": round((move.start.y + move.end.y) / 2, 4),
                           "direction": "positive_x" if dx > 0 else "negative_x", "length": round(abs(dx), 4)})
    pitches = [abs(right["y_center"] - left["y_center"]) for left, right in zip(passes, passes[1:])
               if abs(right["y_center"] - left["y_center"]) > 1e-6]
    return {"pass_count": len(passes), "passes": passes,
            "pitch_values": [round(value, 4) for value in pitches],
            "median_pitch": round(median(pitches), 4) if pitches else None,
            "pitch_unit": "program_units"}


def recognize(program: Program) -> dict[str, Any]:
    geometry = program_geometry(program)
    block_by_id = {block.id: block for block in program.blocks}
    features = []
    for item in geometry["blocks"]:
        block = block_by_id[item["block_id"]]
        kind, confidence, evidence = _classify(item, block.motions)
        if any(move.arc and move.arc.plane_assumed for move in block.motions):
            evidence.append("G17/XY plane inferred for preview; controller default is unverified")
            if confidence.startswith("high"):
                confidence = "medium_assumed_plane_geometry"
        center = None
        if item["bounds"]:
            center = {"x": (item["bounds"]["x_min"]+item["bounds"]["x_max"])/2,
                      "y": (item["bounds"]["y_min"]+item["bounds"]["y_max"])/2}
        feature = {
            "id": f"feature-{block.id}", "block_id": block.id, "kind": kind,
            "confidence": confidence, "evidence": evidence,
            "physical_operation": "unconfirmed", "requires_operator_confirmation": True,
            "source_lines": {"start": item["source_line_start"], "end": item["source_line_end"]},
            "bounds": item["bounds"], "center": center,
            "closed": item["closed"], "depth_values": item["depth_values"],
            "depth_range": [item["depth_min"], item["depth_max"]],
            "tools": item["tools"], "move_ids": item["move_ids"],
            "arc_count": item["arc_count"], "move_count": item["move_count"],
            "geometry_quality": item["bounds"]["quality"] if item["bounds"] else "unresolved",
        }
        if kind == "raster_or_zigzag_candidate":
            raster = _raster_analysis(block.motions)
            feature["raster_analysis"] = raster
            if raster["median_pitch"] is not None:
                feature["evidence"].append(f"median pass pitch {raster['median_pitch']} program units")
        features.append(feature)

    # Group likely repeated small loops by similar size and depth only. This is
    # a geometric pattern hint; it does not infer a physical hole function.
    signatures: dict[tuple[float, float, tuple[float, ...]], list[dict[str, Any]]] = defaultdict(list)
    for feature in features:
        if feature["kind"] != "small_loop_candidate" or not feature["bounds"]:
            continue
        bounds = feature["bounds"]
        signature = (round(bounds["width"], 1), round(bounds["height"], 1), tuple(round(x, 1) for x in feature["depth_values"]))
        signatures[signature].append(feature)
    patterns = []
    for signature, group in signatures.items():
        if len(group) < 2:
            continue
        patterns.append({
            "id": f"repeat-{len(patterns)+1:03d}", "kind": "similar_closed_loop_group",
            "feature_ids": [feature["id"] for feature in group],
            "size_signature": {"width": signature[0], "height": signature[1], "depths": list(signature[2])},
            "confidence": "medium_geometric_similarity", "physical_operation": "unconfirmed",
        })

    loops = [feature for feature in features if feature["kind"] == "small_loop_candidate" and feature["center"]]
    if len(loops) >= 4:
        centers = [feature["center"] for feature in loops]
        symmetry_tol = max(0.5, max((feature["bounds"]["width"] for feature in loops), default=1)*0.15)
        for axis, coordinate in (("vertical_x", (min(c["x"] for c in centers)+max(c["x"] for c in centers))/2),
                                 ("horizontal_y", (min(c["y"] for c in centers)+max(c["y"] for c in centers))/2)):
            paired = set()
            for index, center in enumerate(centers):
                reflected = ({"x": 2*coordinate-center["x"], "y": center["y"]} if axis == "vertical_x"
                             else {"x": center["x"], "y": 2*coordinate-center["y"]})
                for other_index, other in enumerate(centers):
                    if other_index != index and abs(other["x"]-reflected["x"]) <= symmetry_tol and abs(other["y"]-reflected["y"]) <= symmetry_tol:
                        paired.add(index)
                        paired.add(other_index)
                        break
            if len(paired) >= 4:
                patterns.append({
                    "id": f"symmetry-{len(patterns)+1:03d}", "kind": "mirror_symmetry_candidate",
                    "axis": axis, "axis_coordinate": round(coordinate, 3),
                    "feature_ids": [loops[index]["id"] for index in sorted(paired)],
                    "confidence": "medium_geometric_pattern", "physical_operation": "unconfirmed",
                })

    coincident: dict[tuple[float, float, float, float], list[dict[str, Any]]] = defaultdict(list)
    for feature in features:
        bounds = feature["bounds"]
        if bounds:
            key = tuple(round(bounds[key], 2) for key in ("x_min", "y_min", "x_max", "y_max"))
            coincident[key].append(feature)
    for group in coincident.values():
        if len(group) >= 2 and len({tuple(item["depth_values"]) for item in group}) > 1:
            patterns.append({
                "id": f"depth-repeat-{len(patterns)+1:03d}", "kind": "same_xy_path_at_multiple_depths",
                "feature_ids": [feature["id"] for feature in group],
                "depth_values": sorted({value for feature in group for value in feature["depth_values"]}),
                "confidence": "high_geometric_similarity", "physical_operation": "unconfirmed",
            })

    warnings = list(program.warnings)
    if any(feature["kind"] == "large_closed_path_candidate" for feature in features):
        warnings.append("بسته‌بودن مسیر به‌تنهایی ثابت نمی‌کند که مسیر، مرز نهایی قطعه است.")
    if any(feature["kind"] == "raster_or_zigzag_candidate" for feature in features):
        warnings.append("مسیر رفت‌وبرگشتی به‌عنوان الگوی هندسی جدا ثبت شده؛ عملکرد فیزیکی آن تأیید نشده است.")
    return {
        "source_sha256": program.source_sha256,
        "units": ("mm" if program.units == "mm" else
                  "mm (normalized from inch source)" if program.units == "inch" else
                  "unspecified program units"),
        "source_units": program.units or "unspecified",
        "controller": {"status": "unknown", "identified": False},
        "bounds": geometry["bounds"],
        "blocks": geometry["blocks"],
        "features": features,
        "patterns": patterns,
        "warnings": warnings,
        "safe_for_execution": False,
    }
