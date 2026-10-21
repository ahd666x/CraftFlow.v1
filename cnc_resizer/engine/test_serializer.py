"""Package-B regression coverage: fail-closed candidate serializer, the
dialect-bridge preflight and the engine machine-output endpoint.

The serializer may only patch the source text's own lines with changed XY
words; the legacy workshop dialect gate re-verifies the final text. Anything
else stays blocked.
"""
import json
import unittest

from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .. import controller
from .ir import to_dict
from .output_validation import validate_output_candidate
from .parametric import preview_resize
from .parser import parse_program
from .recognition import recognize
from .serializer import SerializerBlocked, serialize_candidate
from ..tests import SAMPLE

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

MACHINE_LIMITS = {"x_min": -50, "x_max": 500, "y_min": -50, "y_max": 2000,
                  "z_min": -30, "z_max": 10, "max_feed": 20000, "max_spindle": 20000}


def closed_stretch_preview(text):
    program = parse_program(text)
    feature = next(item for item in recognize(program)["features"]
                   if item["kind"] == "large_closed_path_candidate")
    return preview_resize(program, {
        "axis": "y", "target_dimension": 230, "reference_start": 20, "reference_end": 180,
        "features": {feature["id"]: "stretch"},
    })


class SerializerTests(SimpleTestCase):
    def test_patches_only_changed_end_words_and_preserves_all_other_lines(self):
        text = ROUNDED_RECT_LF.replace("\n", "\r\n")
        preview = closed_stretch_preview(text)
        serialized = serialize_candidate(text, preview)
        source_lines = text.split("\r\n")
        patched_lines = serialized["text"].split("\r\n")
        self.assertEqual(len(source_lines), len(patched_lines))
        for index, (before, after) in enumerate(zip(source_lines, patched_lines)):
            if index + 1 in serialized["changed_line_numbers"]:
                self.assertNotEqual(before, after)
            else:
                self.assertEqual(before, after, f"line {index + 1} must stay byte-identical")
        self.assertIn("G1 Y220.000", serialized["text"])
        self.assertIn("G3 X120 Y230.000 I-10 J0", serialized["text"])
        self.assertIn("G1 Y10", serialized["text"])
        self.assertTrue(serialized["text"].endswith("\r\n"))
        self.assertEqual(serialized["serializer_adapter_key"], "minimal_xy_router")
        self.assertTrue(serialized["candidate_sha256"])

    def test_reparsed_candidate_geometry_matches_preview_exactly(self):
        preview = closed_stretch_preview(ROUNDED_RECT_LF)
        serialized = serialize_candidate(ROUNDED_RECT_LF, preview)
        reparsed = parse_program(serialized["text"])
        ir_moves = {move["id"]: move for move in preview["program_ir"]["moves"]}
        for move in reparsed.moves:
            ir = ir_moves[move.id]
            for point_name in ("start", "end"):
                point = getattr(move, point_name)
                for axis in "xyz":
                    value = getattr(point, axis) if point else None
                    expected = (ir[point_name] or {}).get(axis)
                    self.assertEqual(value is None, expected is None, f"{move.id}.{point_name}.{axis}")
                    if value is not None:
                        self.assertAlmostEqual(value, expected, msg=f"{move.id}.{point_name}.{axis}")
            if move.arc:
                for axis in "xy":
                    self.assertAlmostEqual(move.arc.center[axis], ir["arc"]["center"][axis])

    def test_tampered_z_is_blocked(self):
        preview = closed_stretch_preview(ROUNDED_RECT_LF)
        preview["program_ir"]["moves"][3]["end"]["z"] = -99.0
        with self.assertRaises(SerializerBlocked) as caught:
            serialize_candidate(ROUNDED_RECT_LF, preview)
        self.assertTrue(any("changed Z" in item for item in caught.exception.blockers))

    def test_changed_motion_semantics_is_blocked(self):
        preview = closed_stretch_preview(ROUNDED_RECT_LF)
        preview["program_ir"]["moves"][3]["motion"] = "arc_cw"
        with self.assertRaises(SerializerBlocked) as caught:
            serialize_candidate(ROUNDED_RECT_LF, preview)
        self.assertTrue(any("semantics" in item for item in caught.exception.blockers))

    def test_sha_mismatch_is_blocked(self):
        preview = closed_stretch_preview(ROUNDED_RECT_LF)
        other = parse_program(PURE_RECT_TEXT)
        preview["source_sha256"] = other.source_sha256
        with self.assertRaises(SerializerBlocked) as caught:
            serialize_candidate(ROUNDED_RECT_LF, preview)
        self.assertTrue(any("sha256 mismatch" in item for item in caught.exception.blockers))

    def test_r_format_arc_cannot_be_patched(self):
        from .serializer import _patch_plan
        text = "\n".join(["G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-2", "G1 X10 Y0", "G2 X30 Y20 R20", ""])
        program = parse_program(text)
        candidate = to_dict(program)
        arc_move = next(move for move in candidate["moves"] if move["arc"])
        arc_move["end"]["x"] = 35.0
        preview = {"validation": {"passed": True, "errors": []},
                   "source_sha256": program.source_sha256, "program_ir": candidate}
        with self.assertRaises(SerializerBlocked) as caught:
            _patch_plan(program, preview)
        self.assertTrue(any("R-format arc" in item for item in caught.exception.blockers))

    def test_inch_source_is_rejected(self):
        text = "\n".join(["G20 G90 G17", "G0 X0 Y0 Z5", "G1 Z-3", "G1 X10 Y0", "G1 Y100", ""])
        program = parse_program(text)
        self.assertEqual(program.units, "inch")
        preview = {"validation": {"passed": True, "errors": []},
                   "source_sha256": program.source_sha256, "program_ir": to_dict(program)}
        with self.assertRaises(SerializerBlocked) as caught:
            serialize_candidate(text, preview)
        self.assertTrue(any("inch" in item for item in caught.exception.blockers))


PURE_RECT_TEXT = "\n".join([
    "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-3",
    "G1 X120 Y0", "G1 Y200", "G1 X0 Y200", "G1 Y0", "",
])


@override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
class DialectBridgePreflightTests(SimpleTestCase):
    def test_identity_candidate_on_real_workshop_sample_is_allowed(self):
        from ..engine.service import preview_program
        with open(SAMPLE, "r", encoding="utf-8") as stream:
            text = stream.read()
        program = parse_program(text, filename="sample.cnc")
        bounds = recognize(program)["bounds"]
        profile = {
            "axis": "y", "target_dimension": bounds["height"],
            "reference_start": bounds["y_min"] + 1, "reference_end": bounds["y_max"] - 1,
            "features": {feature["id"]: "fixed" for feature in recognize(program)["features"]},
        }
        preview = preview_program(text, profile, filename="sample.cnc")
        self.assertTrue(preview["validation"]["passed"], preview["validation"]["errors"])
        report = validate_output_candidate(program, preview, MACHINE_LIMITS,
                                           source_text=text, dialect_gate=controller.postprocess_resize)
        self.assertEqual(report["blockers"], [], report["blockers"])
        self.assertTrue(report["allowed"])
        self.assertTrue(report["machine_output_bridge"]["active"])
        self.assertEqual(report["machine_output_bridge"]["tool"], 2)
        self.assertTrue(report["machine_output_bridge"]["structure_verified"])
        self.assertTrue(report["candidate_sha256"])
        self.assertFalse(report["output_generated"])

    def test_out_of_dialect_source_stays_blocked_with_bridge(self):
        from ..engine.service import preview_program
        tapered = "%\nG90\nG0 X0 Y0\nG1 X200 Y0\nG1 X250 Y300\nG1 X100 Y400\nG1 X30 Y200\nG1 X0 Y0\nG0 X0 Y0\nM30\n%\n"
        program = parse_program(tapered)
        bounds = recognize(program)["bounds"]
        profile = {"axis": "y", "target_dimension": bounds["height"],
                   "reference_start": bounds["y_min"] + 1, "reference_end": bounds["y_max"] - 1,
                   "features": {feature["id"]: "fixed" for feature in recognize(program)["features"]}}
        preview = preview_program(tapered, profile)
        report = validate_output_candidate(program, preview, MACHINE_LIMITS,
                                           source_text=tapered, dialect_gate=controller.postprocess_resize)
        self.assertFalse(report["allowed"])
        self.assertTrue(any("dialect postprocessor is blocked" in item for item in report["blockers"]))
        self.assertFalse(report["machine_output_bridge"]["active"])

    def test_missing_limits_still_block_with_bridge(self):
        from ..engine.service import preview_program
        with open(SAMPLE, "r", encoding="utf-8") as stream:
            text = stream.read()
        program = parse_program(text, filename="sample.cnc")
        bounds = recognize(program)["bounds"]
        profile = {"axis": "y", "target_dimension": bounds["height"],
                   "reference_start": bounds["y_min"] + 1, "reference_end": bounds["y_max"] - 1,
                   "features": {feature["id"]: "fixed" for feature in recognize(program)["features"]}}
        preview = preview_program(text, profile, filename="sample.cnc")
        report = validate_output_candidate(program, preview, None,
                                           source_text=text, dialect_gate=controller.postprocess_resize)
        self.assertFalse(report["allowed"])
        self.assertTrue(any("travel" in item or "limits" in item for item in report["blockers"]))


class EngineOutputViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("cnc-manager-b", password="testpass")
        manager_group = Group.objects.create(name="2")
        cls.manager.groups.add(manager_group)
        cls.regular = User.objects.create_user("cnc-user-b", password="testpass")

    def url(self):
        return reverse("cnc_resizer:engine_output")

    def test_requires_manager(self):
        response = self.client.post(self.url(), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 302)
        self.client.login(username="cnc-user-b", password="testpass")
        response = self.client.post(self.url(), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 403)

    @override_settings(CNC_RESIZER_SHEET_ADAPTER="")
    def test_blocked_without_active_adapter(self):
        self.client.login(username="cnc-manager-b", password="testpass")
        feature = next(item for item in recognize(parse_program(ROUNDED_RECT_LF))["features"]
                       if item["kind"] == "large_closed_path_candidate")
        response = self.client.post(self.url(), data=json.dumps({
            "filename": "door.cnc", "content": ROUNDED_RECT_LF,
            "profile": {"axis": "y", "target_dimension": 230, "reference_start": 20,
                        "reference_end": 180, "features": {feature["id"]: "stretch"}},
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertFalse(payload["allowed"])
        self.assertFalse(payload["safe_for_execution"])
        self.assertTrue(any("پست‌پروسسور" in item for item in payload["blockers"]))

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
    def test_produces_machine_file_for_verified_workshop_candidate(self):
        self.client.login(username="cnc-manager-b", password="testpass")
        features = recognize(parse_program(WORKSHOP_ROUNDED_RECT))["features"]
        rules = {feature["id"]: ("stretch" if feature["kind"] == "large_closed_path_candidate" else "fixed")
                 for feature in features}
        response = self.client.post(self.url(), data=json.dumps({
            "filename": "door.cnc", "content": WORKSHOP_ROUNDED_RECT,
            "profile": {"axis": "y", "target_dimension": 230, "reference_start": 20,
                        "reference_end": 180, "features": rules},
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["allowed"], payload)
        self.assertTrue(payload["safe_for_execution"])
        self.assertIn("G1Y220.000", payload["cnc_text"])
        self.assertIn("G1Y10.000", payload["cnc_text"])   # downward left edge line stays untouched
        self.assertNotIn("G1Y190.000", payload["cnc_text"])
        self.assertEqual(payload["postprocessor"]["tool"], 2)
        self.assertEqual(payload["postprocessor"]["h"], "2")
        self.assertFalse(payload["postprocessor"]["units_injected"])
        # every non-patched line stays byte-identical (CRLF, N-free dialect preserved)
        self.assertEqual(len(payload["cnc_text"].split("\r\n")), len(WORKSHOP_ROUNDED_RECT.split("\r\n")))
        self.assertIn("G53X0Y3950", payload["cnc_text"])
        self.assertIn("M141", payload["cnc_text"])

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
    def test_out_of_band_request_returns_400(self):
        self.client.login(username="cnc-manager-b", password="testpass")
        feature = next(item for item in recognize(parse_program(ROUNDED_RECT_LF))["features"]
                       if item["kind"] == "large_closed_path_candidate")
        response = self.client.post(self.url(), data=json.dumps({
            "filename": "door.cnc", "content": ROUNDED_RECT_LF,
            "profile": {"axis": "y", "target_dimension": 230, "reference_start": 0,
                        "reference_end": 200, "features": {feature["id"]: "stretch"}},
        }), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("arc crosses the reference band", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
