"""Create a compact, read-only stage-one/four report from a supplied CNC file.

The raw program is never included in the report. No resize is applied and no
machine output is generated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .diagnostics import analyze_text
from .engine.parser import parse_program
from .engine.recognition import recognize


def build_target_report(source: Path) -> dict[str, Any]:
    raw = source.read_bytes()
    text = raw.decode("utf-8-sig")
    raw_hash = hashlib.sha256(raw).hexdigest()
    legacy = analyze_text(text, filename=source.name, source_sha256=raw_hash)
    program = parse_program(text, filename=source.name)
    analysis = recognize(program)
    block_fields = (
        "block_id", "source_line_start", "source_line_end", "move_count", "linear_count", "arc_count",
        "depth_min", "depth_max", "depth_values", "tools", "bounds", "closed", "unresolved_moves",
        "connectivity_errors",
    )
    blocks = [{field: block[field] for field in block_fields} for block in analysis["blocks"]]
    feature_fields = (
        "id", "block_id", "kind", "confidence", "source_lines", "bounds", "depth_values", "tools",
        "move_count", "arc_count", "geometry_quality", "requires_operator_confirmation",
    )
    features = []
    for feature in analysis["features"]:
        item = {field: feature[field] for field in feature_fields}
        if "raster_analysis" in feature:
            raster = feature["raster_analysis"]
            item["raster_analysis"] = {key: raster[key] for key in
                                        ("pass_count", "pitch_values", "median_pitch", "pitch_unit")}
        features.append(item)
    raster_features = [item for item in features if item["kind"] == "raster_or_zigzag_candidate"]
    report = {
        "schema_version": "1.1",
        "report_type": "target_cnc_read_only_baseline",
        "input": {
            "filename": source.name, "sha256_raw_bytes": raw_hash, "bytes": len(raw),
            "line_count": len(text.splitlines()), "source_text_modified": False,
            "source_program_included": False,
        },
        "safety": {
            "read_only": True, "machine_output_written": False, "resized_program_generated": False,
            "safe_for_execution": False, "controller_identified": False,
        },
        "legacy_core_baseline": legacy["legacy_core_baseline"],
        "legacy_detector": legacy["legacy_detector_candidates"],
        "modal_and_commands": {
            "units": program.units or "unspecified", "coordinate_mode": program.coordinate_mode,
            "coordinate_system": program.coordinate_system, "plane_explicit": program.plane,
            "observed_g_codes": program.observed_g_codes, "observed_m_codes": program.observed_m_codes,
            "tools": program.tools, "h_registers": program.h_registers,
            "feeds": program.feed_values, "spindle_values": program.spindle_values,
            "unknown_g_codes": program.unknown_g_codes, "unknown_m_codes": program.unknown_m_codes,
            "compact_controller_tokens": program.compact_controller_tokens,
            "machine_coordinate_source_lines": sorted(move.source_line for move in program.moves if move.machine_coordinates),
        },
        "parser_summary": {
            "line_count": program.line_count, "move_count": len(program.moves),
            "path_block_count": len(program.blocks),
            "arc_count": sum(move.arc is not None for move in program.moves),
            "source_sha256_utf8_text": program.source_sha256,
        },
        "blocks": blocks,
        "features": features,
        "patterns": analysis["patterns"],
        "warnings": list(dict.fromkeys(legacy["warnings"] + program.warnings)),
        "acceptance": {
            "narrow_path_separate": sum(item["kind"] == "elongated_path_candidate" for item in features) >= 2,
            "two_surface_pass_features_separate": len(raster_features) >= 2,
            "four_small_loop_candidates": sum(item["kind"] == "small_loop_candidate" for item in features) == 4,
            "large_closed_contour_not_misclassified_as_raster": any(
                item["kind"] == "large_closed_path_candidate" for item in features),
            "raster_pitch_detected": bool(raster_features) and all(
                item.get("raster_analysis", {}).get("median_pitch") == 14.0 for item in raster_features),
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a safe JSON CNC target report without storing the source G-code.")
    parser.add_argument("input", type=Path, help="Path to the source CNC file")
    parser.add_argument("--output", type=Path, required=True, help="Path for the compact JSON report")
    args = parser.parse_args()
    report = build_target_report(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote read-only report: {args.output} ({report['input']['sha256_raw_bytes']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
