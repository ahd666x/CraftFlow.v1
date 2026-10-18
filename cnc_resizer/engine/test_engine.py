"""Regression coverage for the independent CNC engineering engine."""
import json
import unittest
from pathlib import Path

from .geometry import block_geometry
from .parametric import TransformError, preview_resize
from .parser import ParseError, parse_program
from .postprocessor import OutputBlocked, execution_gate, generate_gcode
from .recognition import recognize
from .service import inspect_program
from .ir import to_dict
from .output_validation import validate_output_candidate

SAMPLE = Path(__file__).resolve().parents[1] / "sample.cnc"


class ParserAndGeometryTests(unittest.TestCase):
    def test_sample_parses_to_traceable_ir_without_assuming_units_or_controller(self):
        program = parse_program(SAMPLE.read_text(encoding="utf-8"), filename="sample.cnc")
        self.assertEqual(program.line_count, 231)
        self.assertEqual(program.units, None)
        self.assertEqual(program.tools, [2])
        self.assertEqual(len(program.blocks), 10)
        self.assertEqual(sum(move.motion.startswith("arc_") for move in program.moves), 36)
        self.assertIn({"code": "M141", "source_lines": [5]}, program.unknown_m_codes)
        self.assertIn({"token": "M6T2", "source_lines": [6]}, program.compact_controller_tokens)
        self.assertTrue(all(move.source_line > 0 and move.id for move in program.moves))

    def test_modal_absolute_and_incremental_positions_are_resolved(self):
        program = parse_program("G21 G90\nG0 X0 Y0\nG1 X10 Y20\nG91\nG1 X5 Y-2\n")
        cut = [move for move in program.moves if move.motion == "linear"]
        self.assertEqual((cut[0].end.x, cut[0].end.y), (10, 20))
        self.assertEqual((cut[1].end.x, cut[1].end.y), (15, 18))

    def test_inches_are_normalized_to_mm_when_explicit(self):
        program = parse_program("G20 G90\nG0 X0 Y0\nG1 X1 Y2\n")
        move = next(move for move in program.moves if move.motion == "linear")
        self.assertEqual((program.units, move.end.x, move.end.y), ("inch", 25.4, 50.8))

    def test_conflicting_modal_motion_codes_are_rejected(self):
        with self.assertRaises(ParseError):
            parse_program("G90 G0 G1 X1 Y2\n")

    def test_xy_arc_geometry_has_radius_and_true_quadrant_bounds(self):
        text = "\n".join([
            "G21 G90 G17", "G0 X2 Y0 Z5", "G1 Z-7",
            "G3 X0 Y2 I-2 J0", "G3 X-2 Y0 I0 J-2",
            "G3 X0 Y-2 I2 J0", "G3 X2 Y0 I0 J2", "",
        ])
        program = parse_program(text)
        geometry = block_geometry(program.blocks[0])
        self.assertTrue(geometry["closed"])
        self.assertAlmostEqual(geometry["bounds"]["x_min"], -2)
        self.assertAlmostEqual(geometry["bounds"]["x_max"], 2)
        self.assertAlmostEqual(geometry["bounds"]["y_min"], -2)
        self.assertAlmostEqual(geometry["bounds"]["y_max"], 2)
        self.assertEqual(geometry["bounds"]["quality"], "exact_projected_arc_bounds")

    def test_xz_arc_center_is_parsed_without_treating_it_as_an_xy_arc(self):
        program = parse_program("\n".join(["G21 G90 G18", "G0 X2 Y0 Z0", "G3 X0 Z2 I-2 K0", ""]))
        arc = next(move.arc for move in program.moves if move.arc)
        self.assertEqual(arc.plane, "XZ")
        self.assertTrue(arc.valid)
        self.assertAlmostEqual(arc.radius, 2)

    def test_implicit_plane_is_preview_assumed_but_not_machine_approved(self):
        program = parse_program("\n".join(["G21 G90", "G0 X2 Y0 Z0", "G3 X0 Y2 I-2 J0", ""]))
        arc = next(move.arc for move in program.moves if move.arc)
        self.assertTrue(arc.valid)
        self.assertTrue(arc.plane_assumed)
        self.assertFalse(execution_gate(program, None)["allowed"])
        self.assertEqual(recognize(program)["blocks"][0]["bounds"]["quality"], "assumed_g17_projected_arc_bounds")

    def test_ir_retains_coordinate_mode_source_block_tool_and_h_register(self):
        program = parse_program("\n".join([
            "G21 G90 G17", "G0 X0 Y0", "G43 H3", "G1 Z-1", "G1 X2 Y1 F100 S2000", "M6 T3", "",
        ]))
        cut = next(move for move in program.moves if move.motion == "linear" and move.end.x == 2)
        self.assertEqual(cut.coordinate_mode, "absolute")
        self.assertEqual(cut.source_block, "block-0001")
        self.assertEqual(program.tools, [3])
        self.assertEqual(program.h_registers, [{"value": 3.0, "source_line": 3}])

    def test_macro_expressions_are_not_silently_treated_as_static_geometry(self):
        program = parse_program("\n".join(["G21 G90", "#100=10", "G0 X0 Y0", "G1 X[#100] Y0", ""]))
        self.assertTrue(program.unsupported_constructs)
        gate = execution_gate(program, None)
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("Macro/expression" in reason for reason in gate["reasons"]))

    def test_similar_closed_loops_are_grouped_as_patterns_not_physical_hole_claims(self):
        text = "\n".join([
            "G21 G90 G17", "G0 X2 Y0", "G3 X0 Y2 I-2 J0", "G3 X-2 Y0 I0 J-2",
            "G3 X0 Y-2 I2 J0", "G3 X2 Y0 I0 J2", "G0 X12 Y0",
            "G3 X10 Y2 I-2 J0", "G3 X8 Y0 I0 J-2", "G3 X10 Y-2 I2 J0", "G3 X12 Y0 I0 J2", "",
        ])
        program = parse_program(text)
        result = recognize(program)
        self.assertEqual(len(result["patterns"]), 1)
        self.assertEqual(result["patterns"][0]["kind"], "similar_closed_loop_group")
        self.assertEqual(result["patterns"][0]["physical_operation"], "unconfirmed")

    def test_four_small_loops_are_reported_as_candidates_with_mirror_symmetry(self):
        lines = ["G21 G90 G17"]
        for cx, cy in ((109.167, 149.5), (259.167, 149.5), (259.167, 940.499), (109.167, 940.499)):
            lines.extend([
                f"G0 X{cx+2} Y{cy} Z5", "G1 Z-12",
                f"G3 X{cx} Y{cy+2} I-2 J0", f"G3 X{cx-2} Y{cy} I0 J-2",
                f"G3 X{cx} Y{cy-2} I2 J0", f"G3 X{cx+2} Y{cy} I0 J2",
            ])
        result = recognize(parse_program("\n".join(lines)))
        loops = [feature for feature in result["features"] if feature["kind"] == "small_loop_candidate"]
        self.assertEqual(len(loops), 4)
        self.assertTrue(all(feature["requires_operator_confirmation"] for feature in loops))
        self.assertTrue(any(pattern["kind"] == "mirror_symmetry_candidate" for pattern in result["patterns"]))

    def test_longitudinal_raster_and_same_xy_depth_passes_are_separate_candidates(self):
        text = "\n".join([
            "G21 G90 G17", "G0 X32 Y0 Z5", "G1 Z-7", "G1 Y1094", "G1 X49", "G1 Y0", "G1 X32",
            "G0 X32 Y223 Z5", "G1 Z-7", "G1 X448 Y223", "G1 X32 Y237", "G1 X448 Y251",
            "G1 X32 Y265", "G1 X448 Y279", "G1 X32 Y293",
            "G0 X100 Y0 Z5", "G1 Z-7", "G1 X200 Y0", "G1 Y100",
            "G0 X100 Y0 Z5", "G1 Z-14", "G1 X200 Y0", "G1 Y100", "",
        ])
        result = recognize(parse_program(text))
        kinds = {feature["kind"] for feature in result["features"]}
        self.assertIn("elongated_path_candidate", kinds)
        self.assertIn("raster_or_zigzag_candidate", kinds)
        self.assertTrue(any(pattern["kind"] == "same_xy_path_at_multiple_depths" for pattern in result["patterns"]))
        raster = next(feature for feature in result["features"] if feature["kind"] == "raster_or_zigzag_candidate")
        self.assertAlmostEqual(raster["raster_analysis"]["median_pitch"], 14)
        self.assertGreaterEqual(raster["raster_analysis"]["pass_count"], 4)


class ParametricPreviewTests(unittest.TestCase):
    def setUp(self):
        self.text = "\n".join([
            "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-3",
            "G1 X10 Y0", "G1 X10 Y100", "G1 X20 Y100", "G1 X20 Y0", "G1 X30 Y0", "",
        ])
        self.program = parse_program(self.text)
        self.analysis = recognize(self.program)
        self.feature_id = self.analysis["features"][0]["id"]

    def test_preview_changes_selected_feature_only_and_never_returns_machine_text(self):
        original_ir = to_dict(self.program)
        preview = preview_resize(self.program, {
            "axis": "y", "target_dimension": 130, "reference_start": 0, "reference_end": 100,
            "features": {self.feature_id: "stretch"},
        })
        self.assertTrue(preview["validation"]["passed"], preview["validation"]["errors"])
        self.assertEqual(preview["dimension_after"], 130)
        self.assertEqual(preview["changed_feature_ids"], [self.feature_id])
        self.assertFalse(preview["safe_for_execution"])
        self.assertFalse(preview["validation"]["machine_output_written"])
        self.assertNotIn("cnc_text", preview)
        self.assertEqual(to_dict(self.program), original_ir)

    def test_fixed_feature_causes_target_envelope_mismatch_to_be_reported(self):
        preview = preview_resize(self.program, {
            "axis": "y", "target_dimension": 130, "reference_start": 0, "reference_end": 100,
            "features": {self.feature_id: "fixed"},
        })
        self.assertFalse(preview["validation"]["passed"])
        self.assertTrue(any("does not match requested" in error for error in preview["validation"]["errors"]))

    def test_unknown_feature_mode_is_rejected(self):
        with self.assertRaises(TransformError):
            preview_resize(self.program, {
                "axis": "y", "target_dimension": 130, "reference_start": 0, "reference_end": 100,
                "features": {self.feature_id: "execute"},
            })

    def test_every_feature_requires_an_explicit_operator_rule(self):
        with self.assertRaisesRegex(TransformError, "operator-selected rule"):
            preview_resize(self.program, {
                "axis": "y", "target_dimension": 130, "reference_start": 0, "reference_end": 100,
                "features": {},
            })

    def test_inspection_is_json_serializable_and_never_execution_safe(self):
        report = inspect_program(self.text)
        json.dumps(report)
        self.assertFalse(report["safe_for_execution"])
        self.assertTrue(report["preview_only"])

    def test_rigid_end_dependent_circle_keeps_radius_during_preview(self):
        text = "\n".join([
            "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-2", "G1 X5 Y0", "G1 X5 Y100",
            "G1 X10 Y100", "G0 X22 Y90", "G1 Z-2", "G3 X20 Y92 I-2 J0",
            "G3 X18 Y90 I0 J-2", "G3 X20 Y88 I2 J0", "G3 X22 Y90 I0 J2", "",
        ])
        program = parse_program(text)
        features = recognize(program)["features"]
        line = next(item for item in features if item["block_id"] == "block-0001")
        loop = next(item for item in features if item["kind"] == "small_loop_candidate")
        preview = preview_resize(program, {
            "axis": "y", "target_dimension": 130, "reference_start": 0, "reference_end": 80,
            "features": {line["id"]: "stretch", loop["id"]: "dependent_end"},
        })
        self.assertTrue(preview["validation"]["passed"], preview["validation"]["errors"])
        old_radius = next(move.arc.radius for move in program.moves if move.arc)
        new_radius = next(move["arc"]["radius"] for move in preview["program_ir"]["moves"] if move["arc"])
        self.assertAlmostEqual(old_radius, new_radius)

    def test_dependent_feature_preserves_explicit_spacing_after_anchor_moves(self):
        text = "\n".join([
            "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-2", "G1 X10 Y0", "G1 Y10",
            "G0 X10 Y80", "G1 X20 Y80", "G1 Y85",
            "G0 X10 Y90", "G1 X20 Y90", "G1 Y95", "",
        ])
        program = parse_program(text)
        features = recognize(program)["features"]
        lower, dependent, anchor = features
        preview = preview_resize(program, {
            "axis": "y", "target_dimension": 125, "reference_start": 0, "reference_end": 80,
            "features": {
                lower["id"]: "fixed",
                dependent["id"]: {"mode": "dependent_feature", "anchor_feature_id": anchor["id"]},
                anchor["id"]: "dependent_end",
            },
        })
        self.assertTrue(preview["validation"]["passed"], preview["validation"]["errors"])
        constraints = {item["feature_id"]: item for item in preview["constraints"]}
        self.assertEqual(constraints[dependent["id"]]["offset"], -10)
        self.assertAlmostEqual(constraints[anchor["id"]]["translation"], 30)
        self.assertTrue(constraints[dependent["id"]]["offset_preserved"])

    def test_dependent_feature_cycle_is_rejected(self):
        text = "\n".join(["G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-2", "G1 X10 Y10",
                           "G0 X20 Y20", "G1 X30 Y30", ""])
        program = parse_program(text)
        features = recognize(program)["features"]
        with self.assertRaisesRegex(TransformError, "cycle"):
            preview_resize(program, {
                "axis": "y", "target_dimension": 35, "reference_start": 10, "reference_end": 30,
                "features": {
                    features[0]["id"]: {"mode": "dependent_feature", "anchor_feature_id": features[1]["id"]},
                    features[1]["id"]: {"mode": "dependent_feature", "anchor_feature_id": features[0]["id"]},
                },
            })

    def test_output_preflight_checks_limits_and_always_fails_closed(self):
        program = parse_program(self.text)
        feature_ids = [feature["id"] for feature in recognize(program)["features"]]
        preview = preview_resize(program, {
            "axis": "y", "target_dimension": 130, "reference_start": 0, "reference_end": 100,
            "features": {feature_id: "middle_scale" for feature_id in feature_ids},
        })
        report = validate_output_candidate(program, preview, {
            "x_min": -1, "x_max": 100, "y_min": 0, "y_max": 120,
            "z_min": -10, "z_max": 10, "max_feed": 20000, "max_spindle": 20000,
        })
        self.assertFalse(report["allowed"])
        self.assertFalse(report["output_generated"])
        self.assertTrue(any("Y maximum" in blocker for blocker in report["blockers"]))
        self.assertTrue(any("controller-specific output serializer" in blocker for blocker in report["blockers"]))
        self.assertGreater(report["changed_move_count"], 0)

    def test_controller_gate_refuses_output_without_registered_adapter(self):
        program = parse_program(SAMPLE.read_text(encoding="utf-8"), filename="sample.cnc")
        profile = {"approved": True, "adapter_key": "not-registered",
                   "validated_source_sha256": [program.source_sha256],
                   "supported_m_codes": ["M141"], "supported_compact_tokens": ["M6T2"]}
        gate = execution_gate(program, profile)
        self.assertFalse(gate["allowed"])
        self.assertFalse(gate["output_written"])
        self.assertTrue(any("adapter" in reason for reason in gate["reasons"]))
        with self.assertRaises(OutputBlocked):
            generate_gcode(program, program, profile)


if __name__ == "__main__":
    unittest.main()
