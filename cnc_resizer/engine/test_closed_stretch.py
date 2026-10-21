"""Package-B regression coverage: segment-level stretch of closed paths.

A closed path (door/window contour) cannot be scaled through the reference
band without distorting its corner arcs. The engine now lengthens only the
axial straight moves bridging the band and rigidly translates everything
else; any other shape inside the band fails closed.
"""
import unittest

from .ir import to_dict
from .parametric import TransformError, preview_resize
from .parser import parse_program
from .recognition import recognize

ROUNDED_RECT = "\n".join([
    "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-3",
    "G1 X120 Y0",
    "G3 X130 Y10 I0 J10",
    "G1 Y190",
    "G3 X120 Y200 I-10 J0",
    "G1 X0 Y200",
    "G3 X-10 Y190 I0 J-10",
    "G1 Y10",
    "G3 X0 Y0 I10 J0", "",
])

PURE_RECT = "\n".join([
    "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-3",
    "G1 X120 Y0", "G1 Y200", "G1 X0 Y200", "G1 Y0", "",
])

CIRCLE = "\n".join([
    "G21 G90 G17", "G0 X60 Y0 Z5", "G1 Z-3",
    "G3 X0 Y60 I-60 J0", "G3 X-60 Y0 I0 J-60",
    "G3 X0 Y-60 I60 J0", "G3 X60 Y0 I0 J60", "",
])


def feature_of_kind(program, kind):
    return next(item for item in recognize(program)["features"] if item["kind"] == kind)


class ClosedStretchTests(unittest.TestCase):
    def test_rounded_rectangle_stretches_segment_level_and_keeps_arcs_rigid(self):
        program = parse_program(ROUNDED_RECT)
        original_ir = to_dict(program)
        feature = feature_of_kind(program, "large_closed_path_candidate")
        preview = preview_resize(program, {
            "axis": "y", "target_dimension": 230, "reference_start": 20, "reference_end": 180,
            "features": {feature["id"]: "stretch"},
        })
        self.assertTrue(preview["validation"]["passed"], preview["validation"]["errors"])
        self.assertEqual(preview["dimension_after"], 230.0)
        self.assertEqual(preview["changed_feature_ids"], [feature["id"]])
        constraint = next(item for item in preview["constraints"] if item["feature_id"] == feature["id"])
        self.assertEqual(constraint["strategy"], "segment_level_closed")
        ir_moves = {move["id"]: move for move in preview["program_ir"]["moves"]}
        old_radius = next(move.arc.radius for move in program.moves if move.arc)
        new_radius = next(move["arc"]["radius"] for move in preview["program_ir"]["moves"] if move["arc"])
        self.assertAlmostEqual(old_radius, new_radius)
        bottom = next(move for move in program.moves if move.motion == "linear" and move.end.y == 0 and move.end.x == 120)
        top = next(move for move in program.moves if move.motion == "linear" and move.end.y == 200)
        right_edge = next(move for move in program.moves if move.motion == "linear" and move.end.y == 190)
        left_edge = next(move for move in program.moves if move.motion == "linear" and move.end.y == 10)
        self.assertEqual(ir_moves[bottom.id]["end"]["y"], 0.0)          # rigid below
        self.assertAlmostEqual(ir_moves[top.id]["end"]["y"], 230.0)     # translated above
        self.assertAlmostEqual(ir_moves[right_edge.id]["end"]["y"], 220.0)   # junction follows above
        self.assertEqual(ir_moves[left_edge.id]["end"]["y"], 10.0)      # downward edge keeps its end
        self.assertEqual(ir_moves[left_edge.id]["start"]["y"], 220.0)   # and moves at its start
        self.assertEqual(to_dict(program), original_ir)                 # source IR untouched

    def test_rounded_rectangle_stretches_along_x_too(self):
        program = parse_program(ROUNDED_RECT)
        feature = feature_of_kind(program, "large_closed_path_candidate")
        preview = preview_resize(program, {
            "axis": "x", "target_dimension": 170, "reference_start": 20, "reference_end": 100,
            "features": {feature["id"]: "stretch"},
        })
        self.assertTrue(preview["validation"]["passed"], preview["validation"]["errors"])
        self.assertEqual(preview["dimension_after"], 170.0)
        ir_moves = {move["id"]: move for move in preview["program_ir"]["moves"]}
        right_edge = next(move for move in program.moves if move.motion == "linear" and move.end.y == 190)
        self.assertAlmostEqual(ir_moves[right_edge.id]["start"]["x"], 160.0)

    def test_pure_rectangle_stretches_with_full_height_band(self):
        program = parse_program(PURE_RECT)
        feature = feature_of_kind(program, "large_closed_path_candidate")
        preview = preview_resize(program, {
            "axis": "y", "target_dimension": 260, "reference_start": 0, "reference_end": 200,
            "features": {feature["id"]: "stretch"},
        })
        self.assertTrue(preview["validation"]["passed"], preview["validation"]["errors"])
        self.assertEqual(preview["dimension_after"], 260.0)

    def test_band_crossing_arc_is_rejected(self):
        program = parse_program(ROUNDED_RECT)
        feature = feature_of_kind(program, "large_closed_path_candidate")
        with self.assertRaisesRegex(TransformError, "arc crosses the reference band"):
            preview_resize(program, {
                "axis": "y", "target_dimension": 230, "reference_start": 0, "reference_end": 200,
                "features": {feature["id"]: "stretch"},
            })

    def test_band_crossing_diagonal_is_rejected(self):
        program = parse_program("\n".join([
            "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-3",
            "G1 X120 Y0", "G1 Y90", "G1 X130 Y110", "G1 Y200",
            "G1 X0 Y200", "G1 Y0", "",
        ]))
        feature = feature_of_kind(program, "large_closed_path_candidate")
        with self.assertRaisesRegex(TransformError, "axial straight moves"):
            preview_resize(program, {
                "axis": "y", "target_dimension": 230, "reference_start": 20, "reference_end": 180,
                "features": {feature["id"]: "stretch"},
            })

    def test_full_circle_cannot_be_stretched(self):
        program = parse_program(CIRCLE)
        feature = feature_of_kind(program, "large_closed_path_candidate")
        with self.assertRaisesRegex(TransformError, "arc crosses the reference band"):
            preview_resize(program, {
                "axis": "y", "target_dimension": 150, "reference_start": -20, "reference_end": 20,
                "features": {feature["id"]: "stretch"},
            })

    def test_small_loop_stretch_remains_prohibited(self):
        text = "\n".join([
            "G21 G90 G17", "G0 X2 Y0 Z5", "G1 Z-3",
            "G3 X0 Y2 I-2 J0", "G3 X-2 Y0 I0 J-2",
            "G3 X0 Y-2 I2 J0", "G3 X2 Y0 I0 J2", "",
        ])
        program = parse_program(text)
        feature = feature_of_kind(program, "small_loop_candidate")
        with self.assertRaisesRegex(TransformError, "small closed loop"):
            preview_resize(program, {
                "axis": "y", "target_dimension": 10, "reference_start": -1, "reference_end": 1,
                "features": {feature["id"]: "stretch"},
            })

    def test_middle_scale_remains_prohibited_for_closed_paths(self):
        program = parse_program(ROUNDED_RECT)
        feature = feature_of_kind(program, "large_closed_path_candidate")
        with self.assertRaisesRegex(TransformError, "middle scaling would distort"):
            preview_resize(program, {
                "axis": "y", "target_dimension": 230, "reference_start": 20, "reference_end": 180,
                "features": {feature["id"]: "middle_scale"},
            })


if __name__ == "__main__":
    unittest.main()
