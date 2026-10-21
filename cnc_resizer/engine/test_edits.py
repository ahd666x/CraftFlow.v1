"""Package-E phase-one coverage: interactive whole-block shift edits.

A shift must be a pure coordinate patch of the source (entry rapid's END plus
every XY-bearing cut move's END), leave Z-only modal carries untouched, keep
arc I/J offsets identical, and be proven at runtime by reparsing the patched
draft. Features sharing the same XY path at multiple depths move together or
not at all.
"""
import json
import unittest

from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .parametric import TransformError, preview_edits, preview_resize
from .parser import parse_program
from .recognition import recognize
from .serializer import serialize_candidate
from ..tests import SAMPLE

TWO_BLOCKS_LF = "\n".join([
    "G21 G90 G17",
    "G0 X0 Y0 Z5",
    "G1 Z-2",
    "G1 X100 Y0",
    "G1 X100 Y50",
    "G1 X0 Y50",
    "G1 X0 Y0",
    "G0 Z5",
    "G0 X30 Y60",
    "G1 Z-3",
    "G1 X50 Y60",
    "G1 X50 Y80",
    "G1 X30 Y80",
    "G1 X30 Y60",
    "G0 Z5",
    "G0 X0 Y0",
    "M30",
    "",
])

ROUNDED_RECT_LF = "\n".join([
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

TWO_DEPTH_LF = "\n".join([
    "G21 G90 G17",
    "G0 X10 Y10 Z5",
    "G1 Z-2",
    "G1 X30 Y10",
    "G1 X30 Y30",
    "G1 X10 Y30",
    "G1 X10 Y10",
    "G0 Z5",
    "G0 X10 Y10",
    "G1 Z-4",
    "G1 X30 Y10",
    "G1 X30 Y30",
    "G1 X10 Y30",
    "G1 X10 Y10",
    "G0 Z5",
    "M30",
    "",
])

# Closed loop drawn starting from the top edge: with a mid band the loop's
# entry point itself would move, which closed stretch must refuse.
ENTRY_MOVE_LOOP_LF = "\n".join([
    "G21 G90 G17",
    "G0 X50 Y100 Z5",
    "G1 Z-2",
    "G1 X100 Y100",
    "G3 X110 Y90 I10 J0",
    "G1 Y0",
    "G3 X100 Y-10 I-10 J0",
    "G1 X0 Y-10",
    "G3 X-10 Y0 I0 J10",
    "G1 Y100",
    "G3 X0 Y110 I10 J0",
    "G1 X50 Y100",
    "",
])

# Block entered by a rapid with only an X word (Y stays modal): a Y shift
# cannot be patched on that line and must fail closed.
PARTIAL_ENTRY_LF = "\n".join([
    "G21 G90 G17",
    "G0 X0 Y60 Z5",
    "G1 Z-1",
    "G1 X10 Y60",
    "G1 X10 Y70",
    "G1 X0 Y70",
    "G1 X0 Y60",
    "G0 Z5",
    "G0 X30",
    "G1 Z-2",
    "G1 X50 Y60",
    "G1 X50 Y80",
    "G1 X30 Y80",
    "G1 X30 Y60",
    "G0 Z5",
    "M30",
    "",
])

WORKSHOP_ROUNDED_RECT = "\r\n".join([
    "%", "G90G21G54", "G53Z0", "T2M6", "M141", "M6T2", "G43H2",
    "G0X0.000Y0.000Z5.000M3 S16000", "G1Z-3.000F9000.0",
    "G1X120.000Y0.000",
    "G3X130.000Y10.000I0.000J10.000",
    "G1Y190.000",
    "G3X120.000Y200.000I-10.000J0.000",
    "G1X0.000Y200.000",
    "G3X-10.000Y190.000I0.000J-10.000",
    "G1Y10.000",
    "G3X0.000Y0.000I10.000J0.000",
    "G0Z5.000", "G0X0Y0", "G53Z0", "G53X0Y3950", "M30", "%",
]) + "\r\n"

# Workshop-dialect rectangle without corner arcs: stretchable in X. The two
# vertical lines carry only Y words (X is modal), so a stretch must prove the
# modal carry at runtime instead of patching those lines.
WORKSHOP_RECT = "\r\n".join([
    "%", "G90G21G54", "G53Z0", "T2M6", "M141", "M6T2", "G43H2",
    "G0X0.000Y0.000Z5.000M3 S16000", "G1Z-3.000F9000.0",
    "G1X120.000Y0.000",
    "G1Y200.000",
    "G1X0.000Y200.000",
    "G1Y0.000",
    "G0Z5.000", "G0X0Y0", "G53Z0", "G53X0Y3950", "M30", "%",
]) + "\r\n"

# Triangle: the move from (100,0) to (50,40) is diagonal and would be skewed
# by an X stretch, so the whole edit must fail closed.
DIAGONAL_LF = "\n".join([
    "G21 G90 G17",
    "G0 X0 Y0 Z5",
    "G1 Z-1",
    "G1 X100 Y0",
    "G1 X50 Y40",
    "G1 X0 Y0",
    "G0 Z5",
    "M30",
    "",
])

# Three separated features for phase-two constraints: centres on X are
# 0001 -> 50, 0002 -> 40, 0003 -> 150.
THREE_BLOCKS_LF = "\n".join([
    "G21 G90 G17",
    "G0 X0 Y0 Z5",
    "G1 Z-2",
    "G1 X100 Y0",
    "G1 X100 Y50",
    "G1 X0 Y50",
    "G1 X0 Y0",
    "G0 Z5",
    "G0 X30 Y60",
    "G1 Z-3",
    "G1 X50 Y60",
    "G1 X50 Y80",
    "G1 X30 Y80",
    "G1 X30 Y60",
    "G0 Z5",
    "G0 X140 Y0",
    "G1 Z-2",
    "G1 X160 Y0",
    "G1 X160 Y20",
    "G1 X140 Y20",
    "G1 X140 Y0",
    "G0 Z5",
    "M30",
    "",
])

# The second feature already sits centred on the first in X: a center_on
# constraint on that axis is a no-op and must be refused.
ALIGNED_LF = "\n".join([
    "G21 G90 G17",
    "G0 X0 Y0 Z5",
    "G1 Z-2",
    "G1 X100 Y0",
    "G1 X100 Y50",
    "G1 X0 Y50",
    "G1 X0 Y0",
    "G0 Z5",
    "G0 X40 Y60",
    "G1 Z-3",
    "G1 X60 Y60",
    "G1 X60 Y80",
    "G1 X40 Y80",
    "G1 X40 Y60",
    "G0 Z5",
    "M30",
    "",
])

# Depth-repeat group (blocks 1+2 share one XY path at Z-2/Z-4) plus a third
# independent feature to anchor constraints against.
TWO_DEPTH_PLUS_LF = "\n".join([
    "G21 G90 G17",
    "G0 X10 Y10 Z5",
    "G1 Z-2",
    "G1 X30 Y10",
    "G1 X30 Y30",
    "G1 X10 Y30",
    "G1 X10 Y10",
    "G0 Z5",
    "G0 X10 Y10",
    "G1 Z-4",
    "G1 X30 Y10",
    "G1 X30 Y30",
    "G1 X10 Y30",
    "G1 X10 Y10",
    "G0 Z5",
    "G0 X60 Y10",
    "G1 Z-2",
    "G1 X90 Y10",
    "G1 X90 Y40",
    "G1 X60 Y40",
    "G1 X60 Y10",
    "G0 Z5",
    "M30",
    "",
])


# Two bars that ALREADY partially overlap in the source (a groove crossing a
# raster band, as real panels have): the collision gate must not block edits
# that keep that pre-existing coexistence.
CROSSING_LF = "\n".join([
    "G21 G90 G17",
    "G0 X0 Y0 Z5",
    "G1 Z-2",
    "G1 X100 Y0",
    "G1 X100 Y10",
    "G1 X0 Y10",
    "G1 X0 Y0",
    "G0 Z5",
    "G0 X40 Y-20",
    "G1 Z-2",
    "G1 X50 Y-20",
    "G1 X50 Y30",
    "G1 X40 Y30",
    "G1 X40 Y-20",
    "G0 Z5",
    "M30",
    "",
])


def shift_preview(text, feature_picker, dx, dy):
    program = parse_program(text)
    feature = feature_picker(recognize(program)["features"])
    return preview_edits(program, {"edits": [{"op": "shift", "feature_id": feature["id"], "dx": dx, "dy": dy}]},
                         source_text=text)


def second_feature(features):
    return features[1]


def closed_feature(features):
    return next(item for item in features if item["kind"] == "large_closed_path_candidate")


class PreviewEditsTests(SimpleTestCase):
    def test_shift_moves_whole_block_including_entry_rapid(self):
        result = shift_preview(TWO_BLOCKS_LF, second_feature, 5, 10)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        self.assertEqual(result["edits_applied"][0]["feature_id"], "feature-block-0002")
        original = recognize(parse_program(TWO_BLOCKS_LF))
        before = {feature["id"]: feature["bounds"] for feature in original["features"] if feature["bounds"]}
        after = {feature["id"]: feature["bounds"] for feature in result["resized"]["features"] if feature["bounds"]}
        self.assertAlmostEqual(after["feature-block-0002"]["x_min"], before["feature-block-0002"]["x_min"] + 5)
        self.assertAlmostEqual(after["feature-block-0002"]["y_min"], before["feature-block-0002"]["y_min"] + 10)
        self.assertAlmostEqual(after["feature-block-0002"]["x_max"], before["feature-block-0002"]["x_max"] + 5)
        self.assertAlmostEqual(after["feature-block-0002"]["y_max"], before["feature-block-0002"]["y_max"] + 10)
        self.assertAlmostEqual(after["feature-block-0001"]["x_min"], before["feature-block-0001"]["x_min"])
        self.assertAlmostEqual(after["feature-block-0001"]["y_max"], before["feature-block-0001"]["y_max"])

    def test_arc_ij_offsets_stay_identical_in_patched_text(self):
        result = shift_preview(ROUNDED_RECT_LF, closed_feature, 5, 10)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        serialized = serialize_candidate(ROUNDED_RECT_LF, result)
        self.assertIn("G0 X5.000 Y10.000 Z5", serialized["text"])
        self.assertIn("G3 X135.000 Y20.000 I0 J10", serialized["text"])
        self.assertIn("G3 X5.000 Y10.000 I10 J0", serialized["text"])
        self.assertIn("G1 Z-3", serialized["text"])  # modal plunge stays byte-identical

    def test_patch_touches_only_the_shifted_block_lines(self):
        result = shift_preview(TWO_BLOCKS_LF, second_feature, 5, 10)
        serialized = serialize_candidate(TWO_BLOCKS_LF, result)
        source_lines = TWO_BLOCKS_LF.split("\n")
        patched_lines = serialized["text"].split("\n")
        self.assertEqual(len(source_lines), len(patched_lines))
        touched = set(serialized["changed_line_numbers"])
        self.assertTrue(touched.issubset(set(range(9, 15))), touched)  # block-0002's own lines only
        for index, (before, after) in enumerate(zip(source_lines, patched_lines)):
            if index + 1 not in touched:
                self.assertEqual(before, after, f"line {index + 1} must stay byte-identical")

    def test_unknown_feature_rejected(self):
        program = parse_program(TWO_BLOCKS_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [{"op": "shift", "feature_id": "feature-block-9999", "dx": 1, "dy": 0}]},
                          source_text=TWO_BLOCKS_LF)
        self.assertIn("unknown feature_id", str(caught.exception))

    def test_duplicate_feature_rejected(self):
        program = parse_program(TWO_BLOCKS_LF)
        edit = {"op": "shift", "feature_id": "feature-block-0002", "dx": 1, "dy": 0}
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [edit, dict(edit)]}, source_text=TWO_BLOCKS_LF)
        self.assertIn("edited twice", str(caught.exception))

    def test_zero_delta_rejected(self):
        program = parse_program(TWO_BLOCKS_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [{"op": "shift", "feature_id": "feature-block-0002", "dx": 0, "dy": 0}]},
                          source_text=TWO_BLOCKS_LF)
        self.assertIn("zero", str(caught.exception))

    def test_non_numeric_delta_rejected(self):
        program = parse_program(TWO_BLOCKS_LF)
        with self.assertRaises(TransformError):
            preview_edits(program, {"edits": [{"op": "shift", "feature_id": "feature-block-0002",
                                               "dx": "abc", "dy": 0}]}, source_text=TWO_BLOCKS_LF)

    def test_empty_and_malformed_edits_rejected(self):
        program = parse_program(TWO_BLOCKS_LF)
        for request in ({}, {"edits": []}, {"edits": "nope"}, {"edits": [{"op": "mirror", "feature_id": "x"}]}):
            with self.assertRaises(TransformError):
                preview_edits(program, request, source_text=TWO_BLOCKS_LF)

    def test_depth_repeat_partial_shift_rejected(self):
        program = parse_program(TWO_DEPTH_LF)
        recognition = recognize(program)
        kinds = {pattern["kind"] for pattern in recognition["patterns"]}
        self.assertIn("same_xy_path_at_multiple_depths", kinds)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [{"op": "shift", "feature_id": "feature-block-0001", "dx": 5, "dy": 0}]},
                          source_text=TWO_DEPTH_LF)
        self.assertIn("multiple depths", str(caught.exception))

    def test_depth_repeat_different_deltas_rejected(self):
        program = parse_program(TWO_DEPTH_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [
                {"op": "shift", "feature_id": "feature-block-0001", "dx": 5, "dy": 0},
                {"op": "shift", "feature_id": "feature-block-0002", "dx": 3, "dy": 0},
            ]}, source_text=TWO_DEPTH_LF)
        self.assertIn("identical shift delta", str(caught.exception))

    def test_depth_repeat_identical_delta_allowed(self):
        program = parse_program(TWO_DEPTH_LF)
        result = preview_edits(program, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0001", "dx": 5, "dy": 0},
            {"op": "shift", "feature_id": "feature-block-0002", "dx": 5, "dy": 0},
        ]}, source_text=TWO_DEPTH_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])

    def test_real_workshop_sample_hole_shift_roundtrip(self):
        with open(SAMPLE, "r", encoding="utf-8") as stream:
            text = stream.read()
        result = shift_preview(text, lambda features: features[0], 2, -1)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        serialized = serialize_candidate(text, result)
        self.assertIn("N70G0X115.194Y66.004Z5.000M3 S16000", serialized["text"])
        self.assertIn("N140G3X115.440Y64.477I+0.838J+0.629", serialized["text"])
        self.assertIn("N80G1Z-12.000F15000.0", serialized["text"])  # modal plunge untouched
        self.assertIn("N310G0X17.734Y152.276M3 S16000", serialized["text"])  # next hole untouched
        runtime = parse_program(serialized["text"])
        self.assertEqual(len(runtime.moves), len(parse_program(text).moves))

    def test_sample_outer_contour_x_shift_via_modal_carry(self):
        with open(SAMPLE, "r", encoding="utf-8") as stream:
            text = stream.read()
        # The outer contour's vertical lines carry only Y words (X is modal);
        # an X shift is still runtime-correct because the patched entry rapid
        # moves the modal chain. The runtime reparse proves it.
        result = shift_preview(text, lambda features: features[8], 2, 0)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        serialized = serialize_candidate(text, result)
        self.assertIn("N1990G0X1.500Y111.045M3 S16000", serialized["text"])
        self.assertIn("N2010G1Y1628.955", serialized["text"])  # X-less line stays byte-identical (modal carry)
        self.assertIn("N2020G2X3.306Y1632.300I+4.000J+0.000", serialized["text"])

    def test_partial_word_entry_rapid_blocks_cross_axis_shift(self):
        program = parse_program(PARTIAL_ENTRY_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [{"op": "shift", "feature_id": "feature-block-0002",
                                               "dx": 0, "dy": 5}]}, source_text=PARTIAL_ENTRY_LF)
        self.assertIn("safe coordinate patch", str(caught.exception))
        result = preview_edits(program, {"edits": [{"op": "shift", "feature_id": "feature-block-0002",
                                                    "dx": 4, "dy": 0}]}, source_text=PARTIAL_ENTRY_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])

    def test_sample_outer_contour_y_shift_roundtrip(self):
        with open(SAMPLE, "r", encoding="utf-8") as stream:
            text = stream.read()
        result = shift_preview(text, lambda features: features[8], 0, -2)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        serialized = serialize_candidate(text, result)
        self.assertIn("N1990G0X-0.500Y109.045M3 S16000", serialized["text"])
        self.assertIn("N2010G1Y1626.955", serialized["text"])
        self.assertIn("N2140G1Y1626.955", serialized["text"])  # second depth pass moved with it
        self.assertIn("N2130G1Z-16.000", serialized["text"])  # mid-pass plunge untouched


def resize_preview(text, feature_picker, new_length, anchor="min"):
    program = parse_program(text)
    feature = feature_picker(recognize(program)["features"])
    return preview_edits(program, {"edits": [{"op": "resize_x", "feature_id": feature["id"],
                                              "new_length": new_length, "anchor": anchor}]},
                         source_text=text)


class PreviewResizeEditsTests(SimpleTestCase):
    """Package-E phase-one X resize: segment-level stretch of one feature."""

    def test_resize_x_grows_rectangle_anchored_at_min(self):
        result = resize_preview(TWO_BLOCKS_LF, second_feature, 70)  # 20 -> 70
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = {feature["id"]: feature["bounds"] for feature in result["resized"]["features"]
                 if feature["bounds"]}
        self.assertAlmostEqual(after["feature-block-0002"]["x_min"], 30.0)
        self.assertAlmostEqual(after["feature-block-0002"]["x_max"], 100.0)
        self.assertAlmostEqual(after["feature-block-0002"]["y_min"], 60.0)
        self.assertAlmostEqual(after["feature-block-0002"]["y_max"], 80.0)
        self.assertAlmostEqual(after["feature-block-0001"]["x_max"], 100.0)  # untouched neighbour

    def test_resize_x_grows_big_rectangle_anchored_at_min(self):
        result = resize_preview(TWO_BLOCKS_LF, lambda features: features[0], 130)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = {feature["id"]: feature["bounds"] for feature in result["resized"]["features"]
                 if feature["bounds"]}
        self.assertAlmostEqual(after["feature-block-0001"]["x_min"], 0.0)
        self.assertAlmostEqual(after["feature-block-0001"]["x_max"], 130.0)
        self.assertAlmostEqual(after["feature-block-0001"]["y_min"], 0.0)
        self.assertAlmostEqual(after["feature-block-0001"]["y_max"], 50.0)

    def test_resize_x_anchored_at_max_moves_min_edge_and_entry_rapid(self):
        result = resize_preview(TWO_BLOCKS_LF, lambda features: features[0], 130, anchor="max")
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = {feature["id"]: feature["bounds"] for feature in result["resized"]["features"]
                 if feature["bounds"]}
        self.assertAlmostEqual(after["feature-block-0001"]["x_min"], -30.0)
        self.assertAlmostEqual(after["feature-block-0001"]["x_max"], 100.0)
        serialized = serialize_candidate(TWO_BLOCKS_LF, result)
        self.assertIn("G0 X-30.000 Y0 Z5", serialized["text"])  # entry rapid follows junction 0
        self.assertIn("G1 X-30.000 Y50", serialized["text"])

    def test_resize_x_anchored_at_center(self):
        result = resize_preview(TWO_BLOCKS_LF, lambda features: features[0], 130, anchor="center")
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = {feature["id"]: feature["bounds"] for feature in result["resized"]["features"]
                 if feature["bounds"]}
        self.assertAlmostEqual(after["feature-block-0001"]["x_min"], -15.0)
        self.assertAlmostEqual(after["feature-block-0001"]["x_max"], 115.0)

    def test_resize_x_shrinks(self):
        result = resize_preview(TWO_BLOCKS_LF, lambda features: features[0], 80)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = {feature["id"]: feature["bounds"] for feature in result["resized"]["features"]
                 if feature["bounds"]}
        self.assertAlmostEqual(after["feature-block-0001"]["x_max"], 80.0)

    def test_resize_x_patches_only_axial_lines(self):
        result = resize_preview(TWO_BLOCKS_LF, lambda features: features[0], 130)
        serialized = serialize_candidate(TWO_BLOCKS_LF, result)
        source_lines = TWO_BLOCKS_LF.split("\n")
        patched_lines = serialized["text"].split("\n")
        self.assertEqual(len(source_lines), len(patched_lines))
        touched = set(serialized["changed_line_numbers"])
        self.assertEqual(touched, {4, 5}, touched)  # the two axial moves only
        for index, (before, after) in enumerate(zip(source_lines, patched_lines)):
            if index + 1 not in touched:
                self.assertEqual(before, after, f"line {index + 1} must stay byte-identical")
        self.assertIn("G1 X130.000 Y0", patched_lines[3])
        self.assertIn("G1 X130.000 Y50", patched_lines[4])

    def test_resize_x_rejects_arc_at_extremes(self):
        with self.assertRaises(TransformError) as caught:
            resize_preview(ROUNDED_RECT_LF, closed_feature, 250)
        self.assertIn("arc", str(caught.exception))

    def test_resize_x_rejects_skewed_diagonal(self):
        with self.assertRaises(TransformError) as caught:
            resize_preview(DIAGONAL_LF, lambda features: features[0], 150)
        self.assertIn("skewed", str(caught.exception))

    def test_resize_x_rejects_noop_length(self):
        with self.assertRaises(TransformError) as caught:
            resize_preview(TWO_BLOCKS_LF, lambda features: features[0], 100)
        self.assertIn("current X span", str(caught.exception))

    def test_resize_x_rejects_invalid_length(self):
        program = parse_program(TWO_BLOCKS_LF)
        for new_length in (0, -5, "abc"):
            with self.assertRaises(TransformError):
                preview_edits(program, {"edits": [{"op": "resize_x", "feature_id": "feature-block-0001",
                                                   "new_length": new_length}]}, source_text=TWO_BLOCKS_LF)

    def test_resize_x_rejects_unknown_anchor(self):
        with self.assertRaises(TransformError) as caught:
            resize_preview(TWO_BLOCKS_LF, lambda features: features[0], 130, anchor="left")
        self.assertIn("anchor", str(caught.exception))

    def test_resize_x_depth_repeat_partial_rejected(self):
        program = parse_program(TWO_DEPTH_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [{"op": "resize_x", "feature_id": "feature-block-0001",
                                               "new_length": 40}]}, source_text=TWO_DEPTH_LF)
        self.assertIn("multiple depths", str(caught.exception))

    def test_resize_x_depth_repeat_identical_allowed(self):
        program = parse_program(TWO_DEPTH_LF)
        result = preview_edits(program, {"edits": [
            {"op": "resize_x", "feature_id": "feature-block-0001", "new_length": 40},
            {"op": "resize_x", "feature_id": "feature-block-0002", "new_length": 40},
        ]}, source_text=TWO_DEPTH_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])

    def test_resize_x_depth_repeat_different_lengths_rejected(self):
        program = parse_program(TWO_DEPTH_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [
                {"op": "resize_x", "feature_id": "feature-block-0001", "new_length": 40},
                {"op": "resize_x", "feature_id": "feature-block-0002", "new_length": 60},
            ]}, source_text=TWO_DEPTH_LF)
        self.assertIn("identical resize_x parameters", str(caught.exception))

    def test_depth_repeat_mixed_ops_rejected(self):
        program = parse_program(TWO_DEPTH_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [
                {"op": "shift", "feature_id": "feature-block-0001", "dx": 5, "dy": 0},
                {"op": "resize_x", "feature_id": "feature-block-0002", "new_length": 40},
            ]}, source_text=TWO_DEPTH_LF)
        self.assertIn("must receive the identical", str(caught.exception))

    def test_mixed_shift_and_resize_x_across_blocks_allowed(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "resize_x", "feature_id": "feature-block-0001", "new_length": 120},
            {"op": "shift", "feature_id": "feature-block-0002", "dx": 5, "dy": 3},
        ]}, source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = {feature["id"]: feature["bounds"] for feature in result["resized"]["features"]
                 if feature["bounds"]}
        self.assertAlmostEqual(after["feature-block-0001"]["x_max"], 120.0)
        self.assertAlmostEqual(after["feature-block-0002"]["x_min"], 35.0)
        self.assertAlmostEqual(after["feature-block-0002"]["y_min"], 63.0)

    def test_real_workshop_sample_rejects_loop_resize(self):
        with open(SAMPLE, "r", encoding="utf-8") as stream:
            text = stream.read()
        with self.assertRaises(TransformError) as caught:
            resize_preview(text, closed_feature, 400)
        self.assertIn("arc", str(caught.exception))

    def test_workshop_rectangle_stretch_proves_modal_carry(self):
        result = resize_preview(WORKSHOP_RECT, lambda features: features[0], 150)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = {feature["id"]: feature["bounds"] for feature in result["resized"]["features"]
                 if feature["bounds"]}
        self.assertAlmostEqual(after["feature-block-0001"]["x_min"], 0.0)
        self.assertAlmostEqual(after["feature-block-0001"]["x_max"], 150.0)
        self.assertAlmostEqual(after["feature-block-0001"]["y_max"], 200.0)
        serialized = serialize_candidate(WORKSHOP_RECT, result)
        self.assertIn("G1X150.000Y0.000", serialized["text"])
        self.assertIn("G1Y200.000", serialized["text"])  # X-less vertical: modal carry, byte-identical
        self.assertIn("G1X0.000Y200.000", serialized["text"])  # left edge untouched
        self.assertIn("G0X0.000Y0.000Z5.000M3 S16000", serialized["text"])  # entry rapid untouched


class ClosedStretchEntryGuardTests(SimpleTestCase):
    def test_stretch_rejects_band_that_moves_loop_entry(self):
        program = parse_program(ENTRY_MOVE_LOOP_LF)
        with self.assertRaises(TransformError) as caught:
            preview_resize(program, {"axis": "y", "target_dimension": 150, "reference_start": 20,
                                     "reference_end": 80,
                                     "features": {feature["id"]: "stretch"
                                                  for feature in recognize(program)["features"]}})
        self.assertIn("entry point", str(caught.exception))

    def test_stretch_with_rigid_entry_still_allowed(self):
        result = preview_resize(parse_program(ROUNDED_RECT_LF),
                                {"axis": "y", "target_dimension": 230, "reference_start": 20,
                                 "reference_end": 180,
                                 "features": {feature["id"]: "stretch"
                                              for feature in recognize(parse_program(ROUNDED_RECT_LF))["features"]}})
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])


def bounds_map(result):
    return {feature["id"]: feature["bounds"] for feature in result["resized"]["features"] if feature["bounds"]}


class PreviewConstraintTests(SimpleTestCase):
    """Package-E phase two: declarative constraints resolved into derived
    rigid shifts, proven at runtime by the same reparse gate as shifts."""

    def test_center_on_feature_axis_x(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "center_on", "feature_id": "feature-block-0002",
             "anchor_feature_id": "feature-block-0001", "axis": "x"}]}, source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = bounds_map(result)
        # anchor centre X = 50 -> the moved feature spans 40..60
        self.assertAlmostEqual(after["feature-block-0002"]["x_min"], 40.0)
        self.assertAlmostEqual(after["feature-block-0002"]["x_max"], 60.0)
        self.assertAlmostEqual(after["feature-block-0002"]["y_min"], 60.0)  # other axis untouched
        self.assertAlmostEqual(after["feature-block-0001"]["x_max"], 100.0)  # anchor stays put
        record = result["constraints_applied"][0]
        self.assertEqual(record["rule"], "center_on")
        self.assertEqual(record["anchor_feature_id"], "feature-block-0001")
        self.assertAlmostEqual(record["translation"]["dx"], 10.0)
        self.assertEqual(record["evaluated_against"], "original_geometry")
        derived = [item for item in result["edits_applied"] if item.get("derived_from")]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["op"], "shift")

    def test_center_on_program_envelope_axis_y(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "center_on", "feature_id": "feature-block-0002", "anchor": "envelope", "axis": "y"}]},
            source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = bounds_map(result)
        # envelope Y 0..80 -> centre 40; feature moves 60..80 to 30..50
        self.assertAlmostEqual(after["feature-block-0002"]["y_min"], 30.0)
        self.assertAlmostEqual(after["feature-block-0002"]["y_max"], 50.0)
        self.assertAlmostEqual(after["feature-block-0002"]["x_min"], 30.0)
        self.assertEqual(result["constraints_applied"][0]["anchor"], "envelope")

    def test_center_on_envelope_inside_contour_is_allowed_nesting(self):
        # The envelope-centred feature ends up fully inside block-0001's
        # bounds: containment is legitimate nesting, not a collision.
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "center_on", "feature_id": "feature-block-0002", "anchor": "envelope", "axis": "y"}]},
            source_text=TWO_BLOCKS_LF)
        self.assertEqual(result["collision_check"]["flagged"], [])
        self.assertGreater(result["collision_check"]["checked_pairs"], 0)

    def test_set_gap_after_on_x(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "set_gap", "feature_id": "feature-block-0002",
             "anchor_feature_id": "feature-block-0001", "axis": "x", "side": "after", "gap": 10}]},
            source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = bounds_map(result)
        # anchor x_max 100 + gap 10 -> feature x_min 110
        self.assertAlmostEqual(after["feature-block-0002"]["x_min"], 110.0)
        self.assertAlmostEqual(after["feature-block-0002"]["x_max"], 130.0)
        record = result["constraints_applied"][0]
        self.assertEqual(record["rule"], "set_gap")
        self.assertEqual(record["side"], "after")
        self.assertAlmostEqual(record["gap"], 10.0)

    def test_set_gap_before_on_y(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "set_gap", "feature_id": "feature-block-0001",
             "anchor_feature_id": "feature-block-0002", "axis": "y", "side": "before", "gap": 25}]},
            source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = bounds_map(result)
        # anchor y_min 60 - gap 25 -> feature y_max 35
        self.assertAlmostEqual(after["feature-block-0001"]["y_max"], 35.0)
        self.assertAlmostEqual(after["feature-block-0001"]["y_min"], -15.0)
        runtime_gap = after["feature-block-0002"]["y_min"] - after["feature-block-0001"]["y_max"]
        self.assertAlmostEqual(runtime_gap, 25.0)

    def test_set_gap_zero_touches_and_is_allowed(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "set_gap", "feature_id": "feature-block-0002",
             "anchor_feature_id": "feature-block-0001", "axis": "x", "side": "after", "gap": 0}]},
            source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = bounds_map(result)
        self.assertAlmostEqual(after["feature-block-0002"]["x_min"], 100.0)
        self.assertEqual(result["collision_check"]["flagged"], [])

    def test_set_gap_negative_rejected(self):
        program = parse_program(TWO_BLOCKS_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [
                {"op": "set_gap", "feature_id": "feature-block-0002",
                 "anchor_feature_id": "feature-block-0001", "axis": "x", "side": "after", "gap": -5}]},
                source_text=TWO_BLOCKS_LF)
        self.assertIn("must not be negative", str(caught.exception))

    def test_center_between_is_equidistant(self):
        program = parse_program(THREE_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "center_between", "feature_id": "feature-block-0002",
             "anchor_feature_id": "feature-block-0001", "second_feature_id": "feature-block-0003",
             "axis": "x"}]}, source_text=THREE_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = bounds_map(result)
        centre = (after["feature-block-0002"]["x_min"] + after["feature-block-0002"]["x_max"]) / 2
        self.assertAlmostEqual(centre, 100.0)  # midpoint of anchor centres 50 and 150
        left = centre - 50.0
        right = 150.0 - centre
        self.assertAlmostEqual(left, right)
        record = result["constraints_applied"][0]
        self.assertEqual(record["rule"], "center_between")
        self.assertEqual(record["second_feature_id"], "feature-block-0003")

    def test_shift_and_constraint_compose_across_features(self):
        # The anchor itself is shifted in the same request; the constraint is
        # still evaluated against the anchor's ORIGINAL geometry and the
        # runtime proof proves both facts together.
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0001", "dx": -30, "dy": 0},
            {"op": "center_on", "feature_id": "feature-block-0002",
             "anchor_feature_id": "feature-block-0001", "axis": "x"}]}, source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = bounds_map(result)
        self.assertAlmostEqual(after["feature-block-0001"]["x_min"], -30.0)  # explicit shift applied
        self.assertAlmostEqual(after["feature-block-0002"]["x_min"], 40.0)  # original anchor centre 50
        self.assertAlmostEqual(after["feature-block-0002"]["x_max"], 60.0)

    def test_noop_constraint_rejected(self):
        program = parse_program(ALIGNED_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [
                {"op": "center_on", "feature_id": "feature-block-0002",
                 "anchor_feature_id": "feature-block-0001", "axis": "x"}]}, source_text=ALIGNED_LF)
        self.assertIn("nothing would change", str(caught.exception))

    def test_depth_repeat_group_broadcasts_constraint(self):
        program = parse_program(TWO_DEPTH_PLUS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "center_on", "feature_id": "feature-block-0001",
             "anchor_feature_id": "feature-block-0003", "axis": "y"}]}, source_text=TWO_DEPTH_PLUS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        after = bounds_map(result)
        # group centre Y 20 -> anchor centre Y 25; both members move +5
        self.assertAlmostEqual(after["feature-block-0001"]["y_min"], 15.0)
        self.assertAlmostEqual(after["feature-block-0002"]["y_min"], 15.0)
        self.assertAlmostEqual(after["feature-block-0003"]["y_min"], 10.0)  # anchor untouched
        self.assertEqual(result["constraints_applied"][0]["targets"],
                         ["feature-block-0001", "feature-block-0002"])
        derived = [item for item in result["edits_applied"] if item.get("derived_from")]
        self.assertEqual({item["feature_id"] for item in derived},
                         {"feature-block-0001", "feature-block-0002"})

    def test_depth_repeat_group_explicit_and_constraint_conflict_rejected(self):
        program = parse_program(TWO_DEPTH_PLUS_LF)
        with self.assertRaises(TransformError) as caught:
            preview_edits(program, {"edits": [
                {"op": "shift", "feature_id": "feature-block-0002", "dx": 5, "dy": 0},
                {"op": "center_on", "feature_id": "feature-block-0001",
                 "anchor_feature_id": "feature-block-0003", "axis": "y"}]}, source_text=TWO_DEPTH_PLUS_LF)
        self.assertIn("already moved by another edit", str(caught.exception))

    def test_constraint_validation_failures(self):
        program = parse_program(TWO_BLOCKS_LF)
        cases = [
            ({"op": "center_on", "feature_id": "feature-block-0002",
              "anchor_feature_id": "feature-block-9999", "axis": "x"}, "unknown anchor_feature_id"),
            ({"op": "center_on", "feature_id": "feature-block-0002",
              "anchor_feature_id": "feature-block-0002", "axis": "x"}, "cannot be anchored to itself"),
            ({"op": "center_on", "feature_id": "feature-block-0002", "axis": "x"},
             "needs anchor_feature_id"),
            ({"op": "center_on", "feature_id": "feature-block-0002", "anchor": "envelope",
              "anchor_feature_id": "feature-block-0001", "axis": "x"}, "not both"),
            ({"op": "set_gap", "feature_id": "feature-block-0002", "anchor": "envelope",
              "axis": "x", "side": "after", "gap": 5}, "needs an anchor_feature_id"),
            ({"op": "set_gap", "feature_id": "feature-block-0002",
              "anchor_feature_id": "feature-block-0001", "axis": "x", "gap": 5}, "side must be"),
            ({"op": "center_on", "feature_id": "feature-block-0002",
              "anchor_feature_id": "feature-block-0001", "axis": "z"}, "axis must be"),
            ({"op": "center_between", "feature_id": "feature-block-0002",
              "anchor_feature_id": "feature-block-0001", "second_feature_id": "feature-block-0001",
              "axis": "x"}, "must differ"),
            ({"op": "center_between", "feature_id": "feature-block-0002",
              "anchor_feature_id": "feature-block-0001", "second_feature_id": "feature-block-9999",
              "axis": "x"}, "unknown second_feature_id"),
        ]
        for edit, expected in cases:
            with self.assertRaises(TransformError) as caught:
                preview_edits(program, {"edits": [edit]}, source_text=TWO_BLOCKS_LF)
            self.assertIn(expected, str(caught.exception), edit)


class PreviewCollisionTests(SimpleTestCase):
    """Phase-two bounds-level collision gate: partial overlap fails closed,
    containment and exact touching are legitimate."""

    def test_partial_overlap_rejected(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0002", "dx": 0, "dy": -25}]},
            source_text=TWO_BLOCKS_LF)
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(len(result["collision_check"]["flagged"]), 1)
        flagged = result["collision_check"]["flagged"][0]
        self.assertEqual(set(flagged["features"]), {"feature-block-0001", "feature-block-0002"})
        self.assertGreater(flagged["overlap_x"], 0)
        self.assertGreater(flagged["overlap_y"], 0)
        self.assertIn("partially overlap", result["validation"]["errors"][0])

    def test_containment_allowed(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0002", "dx": 0, "dy": -30}]},
            source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        self.assertEqual(result["collision_check"]["flagged"], [])

    def test_exact_touching_allowed(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0002", "dx": 70, "dy": -30}]},
            source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        self.assertEqual(result["collision_check"]["flagged"], [])

    def test_depth_group_members_never_flagged(self):
        program = parse_program(TWO_DEPTH_LF)
        result = preview_edits(program, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0001", "dx": 5, "dy": 0},
            {"op": "shift", "feature_id": "feature-block-0002", "dx": 5, "dy": 0},
        ]}, source_text=TWO_DEPTH_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        # the intra-group pair (identical bounds) is excluded, not counted
        self.assertEqual(result["collision_check"]["checked_pairs"], 0)
        self.assertEqual(result["collision_check"]["flagged"], [])

    def test_untouched_pairs_not_rechecked(self):
        # THREE_BLOCKS has three mutually disjoint features; moving none of
        # the pairs through an edit means zero pairs are examined.
        program = parse_program(THREE_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "center_on", "feature_id": "feature-block-0002",
             "anchor_feature_id": "feature-block-0001", "axis": "x"}]}, source_text=THREE_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        self.assertEqual(result["collision_check"]["checked_pairs"], 2)  # moved feature vs both others
        self.assertEqual(result["collision_check"]["flagged"], [])

    def test_preexisting_partial_overlap_not_blocked(self):
        # The two bars overlap in the SOURCE (designer's intent); a small
        # shift that keeps that coexistence must pass, with the pre-existing
        # pair reported as skipped rather than flagged.
        program = parse_program(CROSSING_LF)
        result = preview_edits(program, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0002", "dx": 5, "dy": 0}]},
            source_text=CROSSING_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        self.assertEqual(result["collision_check"]["flagged"], [])
        self.assertEqual(result["collision_check"]["preexisting_skipped"], 1)

    def test_new_overlap_with_third_feature_still_rejected(self):
        # Pre-existing overlap between bars 1 and 3 is tolerated, but the
        # edited bar must not CREATE a new overlap with a clean feature.
        crossing_plus = CROSSING_LF.replace(
            "G0 Z5",
            "G0 Z5\nG0 X160 Y40\nG1 Z-2\nG1 X200 Y40\nG1 X200 Y60\nG1 X160 Y60\nG1 X160 Y40\nG0 Z5",
            1)
        program = parse_program(crossing_plus)
        # feature ids: 0001 = bar 1, 0002 = clean rectangle (160..200, 40..60),
        # 0003 = the crossing bar; move bar 0003 onto the clean rectangle.
        result = preview_edits(program, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0003", "dx": 120, "dy": 55}]},
            source_text=crossing_plus)
        # runtime bar lands on (160..170, 35..85): partial overlap with the
        # clean rectangle (160..200, 40..60) is NEW -> rejected.
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(len(result["collision_check"]["flagged"]), 1)
        self.assertEqual(set(result["collision_check"]["flagged"][0]["features"]),
                         {"feature-block-0002", "feature-block-0003"})
        self.assertIn("newly partially overlap", result["validation"]["errors"][0])


class EngineEditsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("cnc-manager-e", password="testpass")
        manager_group = Group.objects.create(name="2")
        cls.manager.groups.add(manager_group)
        cls.regular = User.objects.create_user("cnc-user-e", password="testpass")

    def preview_url(self):
        return reverse("cnc_resizer:engine_preview")

    def output_url(self):
        return reverse("cnc_resizer:engine_output")

    def edits_payload(self, **extra):
        payload = {"filename": "parts.cnc", "content": TWO_BLOCKS_LF,
                   "edits": [{"op": "shift", "feature_id": "feature-block-0002", "dx": 5, "dy": 10}]}
        payload.update(extra)
        return payload

    def test_requires_manager(self):
        response = self.client.post(self.preview_url(), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 302)
        self.client.login(username="cnc-user-e", password="testpass")
        response = self.client.post(self.preview_url(), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_preview_endpoint_accepts_edits(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        response = self.client.post(self.preview_url(), data=json.dumps(self.edits_payload()),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["operation"], "interactive_edits")
        self.assertTrue(payload["validation"]["passed"], payload["validation"]["errors"])
        self.assertNotIn("operator_review", payload)

    def test_endpoints_reject_combined_edits_and_profile(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        response = self.client.post(self.preview_url(), data=json.dumps(self.edits_payload(profile={})),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be combined", response.json()["error"])

    @override_settings(CNC_RESIZER_SHEET_ADAPTER="")
    def test_output_blocked_without_active_adapter(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        response = self.client.post(self.output_url(), data=json.dumps(self.edits_payload()),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertFalse(payload["allowed"])
        self.assertTrue(any("پست‌پروسسور" in item for item in payload["blockers"]))

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
    def test_output_produces_machine_file_from_edits(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        feature = closed_feature(recognize(parse_program(WORKSHOP_ROUNDED_RECT))["features"])
        payload = {"filename": "door.cnc", "content": WORKSHOP_ROUNDED_RECT,
                   "edits": [{"op": "shift", "feature_id": feature["id"], "dx": 3, "dy": 4}]}
        response = self.client.post(self.output_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["allowed"], body)
        self.assertTrue(body["safe_for_execution"])
        self.assertIn("G0X3.000Y4.000Z5.000M3 S16000", body["cnc_text"])
        self.assertIn("G1X123.000Y4.000", body["cnc_text"])
        self.assertIn("G3X133.000Y14.000I0.000J10.000", body["cnc_text"])  # I/J untouched
        self.assertIn("G1Y194.000", body["cnc_text"])
        self.assertIn("G1Z-3.000F9000.0", body["cnc_text"])  # modal plunge untouched
        self.assertIn("G0X0Y0", body["cnc_text"])  # return home untouched
        self.assertEqual(len(body["cnc_text"].split("\r\n")), len(WORKSHOP_ROUNDED_RECT.split("\r\n")))

    def test_output_400_on_transform_error(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        payload = self.edits_payload()
        payload["edits"][0]["feature_id"] = "feature-block-9999"
        response = self.client.post(self.output_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown feature_id", response.json()["error"])

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
    def test_output_produces_machine_file_from_resize_x(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        payload = {"filename": "plate.cnc", "content": WORKSHOP_RECT,
                   "edits": [{"op": "resize_x", "feature_id": "feature-block-0001",
                              "new_length": 150, "anchor": "min"}]}
        response = self.client.post(self.output_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["allowed"], body)
        self.assertTrue(body["safe_for_execution"])
        self.assertIn("G1X150.000Y0.000", body["cnc_text"])
        self.assertIn("G1Y200.000", body["cnc_text"])  # modal-carry line stays byte-identical
        self.assertIn("G0X0.000Y0.000Z5.000M3 S16000", body["cnc_text"])  # entry rapid untouched
        self.assertEqual(len(body["cnc_text"].split("\r\n")), len(WORKSHOP_RECT.split("\r\n")))

    def test_preview_endpoint_400_on_invalid_resize_x(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        payload = {"filename": "parts.cnc", "content": TWO_BLOCKS_LF,
                   "edits": [{"op": "resize_x", "feature_id": "feature-block-0001",
                              "new_length": 130, "anchor": "left"}]}
        response = self.client.post(self.preview_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("anchor", response.json()["error"])

    def test_preview_endpoint_accepts_constraint_op(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        payload = {"filename": "parts.cnc", "content": TWO_BLOCKS_LF,
                   "edits": [{"op": "center_on", "feature_id": "feature-block-0002",
                              "anchor_feature_id": "feature-block-0001", "axis": "x"}]}
        response = self.client.post(self.preview_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["operation"], "interactive_edits")
        self.assertTrue(body["validation"]["passed"], body["validation"]["errors"])
        self.assertEqual(body["constraints_applied"][0]["rule"], "center_on")
        self.assertIn("collision_check", body)
        self.assertEqual(body["collision_check"]["flagged"], [])

    def test_preview_endpoint_400_on_invalid_constraint(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        payload = {"filename": "parts.cnc", "content": TWO_BLOCKS_LF,
                   "edits": [{"op": "set_gap", "feature_id": "feature-block-0002",
                              "anchor_feature_id": "feature-block-0001", "axis": "x",
                              "side": "after", "gap": -5}]}
        response = self.client.post(self.preview_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("must not be negative", response.json()["error"])

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
    def test_output_blocked_by_collision(self):
        self.client.login(username="cnc-manager-e", password="testpass")
        payload = {"filename": "parts.cnc", "content": TWO_BLOCKS_LF,
                   "edits": [{"op": "shift", "feature_id": "feature-block-0002", "dx": 0, "dy": -25}]}
        response = self.client.post(self.output_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertFalse(body["allowed"])
        self.assertFalse(body["safe_for_execution"])
        self.assertTrue(any("partially overlap" in item for item in body["blockers"]), body["blockers"])


# ---------------------------------------------------------------------------
# Package-E phase three: linear arrays and mirroring.

ARRAY_SOURCE_LF = "\n".join([
    "G21 G90 G17",
    "G0 X10 Y10 Z5",
    "G1 Z-2",
    "G1 X30 Y10",
    "G1 X30 Y30",
    "G1 X10 Y30",
    "G1 X10 Y10",
    "G0 Z5",
    "G0 X60 Y10",
    "G1 Z-2",
    "G1 X90 Y10",
    "G1 X90 Y40",
    "G1 X60 Y40",
    "G1 X60 Y10",
    "G0 Z5",
    "M30",
    "",
])

# Workshop-dialect fixture for the array output gate: two blocks, N×10
# numbering, CRLF endings, M141 preamble and G53 park.
WORKSHOP_ARRAY_CRLF = "\r\n".join([
    "%", "G90G21G54", "G53Z0", "T2M6", "M141", "M6T2", "G43H2",
    "N10G0X10.000Y10.000Z5.000M3 S16000",
    "N20G1Z-2.000F9000.0",
    "N30G1X30.000Y10.000",
    "N40G1X30.000Y30.000",
    "N50G1X10.000Y30.000",
    "N60G1X10.000Y10.000",
    "N70G0Z5.000",
    "N80G0X60.000Y10.000",
    "N90G1Z-2.000F9000.0",
    "N100G1X90.000Y10.000",
    "N110G1X90.000Y40.000",
    "N120G1X60.000Y40.000",
    "N130G1X60.000Y10.000",
    "N140G0Z5.000",
    "G0X0Y0", "G53Z0", "G53X0Y3950", "M30", "%",
]) + "\r\n"

# Arc move without an explicit G2/G3 word on its line (modal arc): mirroring
# cannot flip the direction fail-closed.
MODAL_ARC_LF = "\n".join([
    "G21 G90 G17",
    "G0 X0 Y0 Z5",
    "G1 Z-2",
    "G2 X20 Y0 I10 J0",
    "X40 Y0 I10 J0",
    "G0 Z5",
    "M30",
    "",
])

# Cutter compensation flips tool side under mirroring and must be rejected.
CUTTER_COMP_LF = "\n".join([
    "G21 G90 G17",
    "G0 X0 Y0 Z5",
    "G1 Z-2",
    "G41 X10 Y0",
    "G1 X30 Y0",
    "G40 X40 Y0",
    "G0 Z5",
    "M30",
    "",
])

# Zero X extent: mirroring around the feature's own centre is a no-op on X.
VERTICAL_LINE_LF = "\n".join([
    "G21 G90 G17",
    "G0 X10 Y10 Z5",
    "G1 Z-2",
    "G1 X10 Y30",
    "G0 Z5",
    "M30",
    "",
])


def array_preview(text, feature_id, axis="x", count=2, pitch=25.0):
    program = parse_program(text)
    return preview_edits(program, {"edits": [{"op": "array", "feature_id": feature_id,
                                             "axis": axis, "count": count, "pitch": pitch}]},
                         source_text=text)


def mirror_preview(text, feature_id, axis="x"):
    program = parse_program(text)
    return preview_edits(program, {"edits": [{"op": "mirror", "feature_id": feature_id, "axis": axis}]},
                         source_text=text)


class ArrayPreviewTests(SimpleTestCase):
    def test_array_x_places_copies_right_after_the_source_block(self):
        result = array_preview(ARRAY_SOURCE_LF, "feature-block-0001")
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        runtime_features = result["resized"]["features"]
        self.assertEqual(len(runtime_features), 4)  # 2 source + 2 copies
        spans = sorted(round(f["bounds"]["x_min"], 3) for f in runtime_features
                       if abs(f["bounds"]["y_min"] - 10) < 1e-6 and f["bounds"]["x_max"] - f["bounds"]["x_min"] < 21)
        self.assertEqual(spans, [10.0, 35.0, 60.0])  # source, copy@+25, untouched rect start
        self.assertEqual(result["array_applied"][0]["targets"], ["feature-block-0001"])

    def test_array_negative_pitch_goes_opposite(self):
        result = array_preview(ARRAY_SOURCE_LF, "feature-block-0001", pitch=-25)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        lows = sorted(round(f["bounds"]["x_min"], 3) for f in result["resized"]["features"]
                      if f["bounds"]["x_max"] - f["bounds"]["x_min"] < 21
                      and abs(f["bounds"]["y_max"] - f["bounds"]["y_min"] - 20) < 1e-6)
        self.assertIn(-15.0, lows)

    def test_array_y_axis(self):
        result = array_preview(ARRAY_SOURCE_LF, "feature-block-0001", axis="y", pitch=40)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        tops = sorted(round(f["bounds"]["y_min"], 3) for f in result["resized"]["features"]
                      if f["bounds"]["x_max"] - f["bounds"]["x_min"] < 21)
        self.assertEqual(tops, [10.0, 50.0, 90.0])

    def test_array_depth_repeat_group_broadcasts_to_every_member(self):
        result = array_preview(TWO_DEPTH_LF, "feature-block-0001", pitch=40)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        self.assertEqual(sorted(result["array_applied"][0]["targets"]),
                         ["feature-block-0001", "feature-block-0002"])
        # every depth pass got its own copy at +40
        squares = sorted(round(f["bounds"]["x_min"], 3) for f in result["resized"]["features"]
                         if f["bounds"]["x_max"] - f["bounds"]["x_min"] < 21)
        self.assertEqual(squares, [10.0, 10.0, 50.0, 50.0, 90.0, 90.0])

    def test_array_rejects_invalid_parameters(self):
        cases = [
            {"axis": "x", "count": 0, "pitch": 10},
            {"axis": "x", "count": 2.5, "pitch": 10},
            {"axis": "x", "count": True, "pitch": 10},
            {"axis": "x", "count": 65, "pitch": 10},
            {"axis": "x", "count": 2, "pitch": 0},
            {"axis": "z", "count": 2, "pitch": 10},
            {"count": 2, "pitch": 10},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                program = parse_program(ARRAY_SOURCE_LF)
                edit = {"op": "array", "feature_id": "feature-block-0001", **extra}
                with self.assertRaises(TransformError):
                    preview_edits(program, {"edits": [edit]}, source_text=ARRAY_SOURCE_LF)

    def test_array_conflicts_with_other_edits_on_depth_group(self):
        program = parse_program(TWO_DEPTH_LF)
        with self.assertRaises(TransformError) as ctx:
            preview_edits(program, {"edits": [
                {"op": "shift", "feature_id": "feature-block-0002", "dx": 5, "dy": 0},
                {"op": "array", "feature_id": "feature-block-0001", "axis": "x", "count": 2, "pitch": 40},
            ]}, source_text=TWO_DEPTH_LF)
        self.assertIn("already edited", str(ctx.exception))
        with self.assertRaises(TransformError) as ctx:
            preview_edits(program, {"edits": [
                {"op": "array", "feature_id": "feature-block-0001", "axis": "x", "count": 2, "pitch": 40},
                {"op": "shift", "feature_id": "feature-block-0002", "dx": 5, "dy": 0},
            ]}, source_text=TWO_DEPTH_LF)
        self.assertIn("already edited", str(ctx.exception))

    def test_array_twice_on_same_feature_rejected(self):
        program = parse_program(ARRAY_SOURCE_LF)
        with self.assertRaises(TransformError) as ctx:
            preview_edits(program, {"edits": [
                {"op": "array", "feature_id": "feature-block-0001", "axis": "x", "count": 2, "pitch": 40},
                {"op": "array", "feature_id": "feature-block-0001", "axis": "x", "count": 3, "pitch": 50},
            ]}, source_text=ARRAY_SOURCE_LF)
        self.assertIn("edited twice", str(ctx.exception))

    def test_array_pitch_smaller_than_feature_rejected_by_collision(self):
        result = array_preview(ARRAY_SOURCE_LF, "feature-block-0001", pitch=10)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("newly partially overlap" in item for item in result["validation"]["errors"]))
        self.assertGreaterEqual(len(result["collision_check"]["flagged"]), 1)
        self.assertEqual(len(result["collision_check"]["flagged"][0]["features"]), 2)

    def test_array_same_pitch_replicates_a_preexisting_crossing(self):
        # the bar crosses the band in the SOURCE; arraying both by the same
        # pitch keeps that relative arrangement -> replicated, not blocked
        program = parse_program(CROSSING_LF)
        result = preview_edits(program, {"edits": [
            {"op": "array", "feature_id": "feature-block-0001", "axis": "x", "count": 1, "pitch": 200},
            {"op": "array", "feature_id": "feature-block-0002", "axis": "x", "count": 1, "pitch": 200},
        ]}, source_text=CROSSING_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        self.assertGreaterEqual(result["collision_check"]["replicated_skipped"], 1)

    def test_array_copy_landing_on_a_third_feature_rejected(self):
        program = parse_program(THREE_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "array", "feature_id": "feature-block-0003", "axis": "x", "count": 2, "pitch": -45},
        ]}, source_text=THREE_BLOCKS_LF)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("newly partially overlap" in item for item in result["validation"]["errors"]))

    def test_array_and_shift_compose_across_features(self):
        program = parse_program(ARRAY_SOURCE_LF)
        result = preview_edits(program, {"edits": [
            {"op": "array", "feature_id": "feature-block-0001", "axis": "x", "count": 2, "pitch": 25},
            {"op": "shift", "feature_id": "feature-block-0002", "dx": 0, "dy": 100},
        ]}, source_text=ARRAY_SOURCE_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])


class MirrorPreviewTests(SimpleTestCase):
    def test_mirror_x_keeps_bounds_and_reverses_arcs(self):
        result = mirror_preview(ROUNDED_RECT_LF, "feature-block-0001", axis="x")
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        runtime = result["resized"]["features"][0]
        self.assertAlmostEqual(runtime["bounds"]["x_min"], -10, places=6)
        self.assertAlmostEqual(runtime["bounds"]["x_max"], 130, places=6)
        self.assertEqual(runtime["arc_count"], 4)

    def test_mirror_y(self):
        result = mirror_preview(ROUNDED_RECT_LF, "feature-block-0001", axis="y")
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        runtime = result["resized"]["features"][0]
        self.assertAlmostEqual(runtime["bounds"]["y_min"], 0, places=6)
        self.assertAlmostEqual(runtime["bounds"]["y_max"], 200, places=6)

    def test_mirror_noop_rejected(self):
        program = parse_program(VERTICAL_LINE_LF)
        with self.assertRaises(TransformError) as ctx:
            preview_edits(program, {"edits": [
                {"op": "mirror", "feature_id": "feature-block-0001", "axis": "x"}]},
                source_text=VERTICAL_LINE_LF)
        self.assertIn("no-op", str(ctx.exception))

    def test_mirror_modal_arc_rejected(self):
        program = parse_program(MODAL_ARC_LF)
        with self.assertRaises(TransformError) as ctx:
            preview_edits(program, {"edits": [
                {"op": "mirror", "feature_id": "feature-block-0001", "axis": "x"}]},
                source_text=MODAL_ARC_LF)
        self.assertIn("explicit G2/G3", str(ctx.exception))

    def test_mirror_cutter_compensation_rejected(self):
        program = parse_program(CUTTER_COMP_LF)
        with self.assertRaises(TransformError) as ctx:
            preview_edits(program, {"edits": [
                {"op": "mirror", "feature_id": "feature-block-0001", "axis": "x"}]},
                source_text=CUTTER_COMP_LF)
        self.assertIn("cutter compensation", str(ctx.exception))

    def test_mirror_depth_repeat_group_broadcasts(self):
        result = mirror_preview(TWO_DEPTH_LF, "feature-block-0001", axis="x")
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        self.assertEqual(len([item for item in result["edits_applied"] if item["op"] == "mirror"]), 2)

    def test_mirror_and_shift_compose_across_features(self):
        program = parse_program(TWO_BLOCKS_LF)
        result = preview_edits(program, {"edits": [
            {"op": "mirror", "feature_id": "feature-block-0002", "axis": "x"},
            {"op": "shift", "feature_id": "feature-block-0001", "dx": 5, "dy": 0},
        ]}, source_text=TWO_BLOCKS_LF)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])


class EnginePhaseThreeViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("cnc-manager-p3", password="testpass")
        manager_group = Group.objects.create(name="2")
        cls.manager.groups.add(manager_group)

    def preview_url(self):
        return reverse("cnc_resizer:engine_preview")

    def output_url(self):
        return reverse("cnc_resizer:engine_output")

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
    def test_output_endpoint_array_structural_gate(self):
        self.client.login(username="cnc-manager-p3", password="testpass")
        payload = {"filename": "array.cnc", "content": WORKSHOP_ARRAY_CRLF,
                   "edits": [{"op": "array", "feature_id": "feature-block-0001",
                              "axis": "x", "count": 2, "pitch": 50}]}
        response = self.client.post(self.output_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["allowed"], body)
        self.assertTrue(body["safe_for_execution"])
        self.assertEqual(body["postprocessor"]["gate"], "structural_with_declared_insertions")
        self.assertEqual(body["inserted_line_count"], 14)  # two copies of a 7-line block
        text = body["cnc_text"]
        self.assertEqual(len(text.split("\r\n")), len(WORKSHOP_ARRAY_CRLF.split("\r\n")) + 14)
        self.assertIn("N30G1X30.000Y10.000", text)  # original untouched
        self.assertIn("G1X80.000Y10.000", text)  # copy line translated by +50
        self.assertIn("G1X130.000Y10.000", text)  # second copy translated by +100
        self.assertIn("G0X60.000Y10.000Z5.000M3 S16000", text)  # copy entry rapid
        self.assertNotIn("N30G1X80.000", text)  # copy lines carry no N word

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
    def test_output_endpoint_array_collision_blocked(self):
        self.client.login(username="cnc-manager-p3", password="testpass")
        payload = {"filename": "array.cnc", "content": WORKSHOP_ARRAY_CRLF,
                   "edits": [{"op": "array", "feature_id": "feature-block-0001",
                              "axis": "x", "count": 2, "pitch": 10}]}
        response = self.client.post(self.output_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertFalse(body["allowed"])
        self.assertTrue(any("overlap" in item for item in body["blockers"]), body["blockers"])

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
    def test_output_endpoint_mirror_patches_g_words_and_ij(self):
        self.client.login(username="cnc-manager-p3", password="testpass")
        payload = {"filename": "door.cnc", "content": WORKSHOP_ROUNDED_RECT,
                   "edits": [{"op": "mirror", "feature_id": "feature-block-0001", "axis": "x"}]}
        response = self.client.post(self.output_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["allowed"], body)
        text = body["cnc_text"]
        self.assertIn("G0X120.000Y0.000Z5.000M3 S16000", text)  # entry mirrored around cx=60
        self.assertIn("G1X0.000Y0.000", text)
        self.assertIn("G2X-10.000Y10.000I0.000J10.000", text)  # G3->G2, I flipped, J kept
        self.assertIn("G1Y190.000", text)  # modal X line byte-identical
        self.assertIn("G2X120.000Y0.000I-10.000J0.000", text)  # closing arc
        self.assertEqual(len(text.split("\r\n")), len(WORKSHOP_ROUNDED_RECT.split("\r\n")))

    def test_preview_endpoint_accepts_array_and_mirror(self):
        self.client.login(username="cnc-manager-p3", password="testpass")
        payload = {"filename": "parts.cnc", "content": ARRAY_SOURCE_LF,
                   "edits": [{"op": "array", "feature_id": "feature-block-0001",
                              "axis": "x", "count": 2, "pitch": 25},
                             {"op": "mirror", "feature_id": "feature-block-0002", "axis": "x"}]}
        response = self.client.post(self.preview_url(), data=json.dumps(payload),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["validation"]["passed"], body["validation"]["errors"])
        self.assertEqual(body["array_applied"][0]["count"], 2)
        self.assertEqual(len(body["resized"]["features"]), 4)
        self.assertTrue(any("Array copies" in warning for warning in body["validation"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
