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


if __name__ == "__main__":
    unittest.main()
