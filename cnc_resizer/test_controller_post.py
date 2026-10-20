"""Tests for the workshop controller post-processor (cnc_resizer.controller).

Run with:  python manage.py test cnc_resizer.test_controller_post
"""
from django.test import SimpleTestCase, override_settings

from . import controller, core
from .tests import SAMPLE
import os


def read_sample():
    with open(SAMPLE, "r", encoding="utf-8") as stream:
        return stream.read()


@override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
class VerifyProgramTests(SimpleTestCase):
    def test_verified_dialect_passes_with_implicit_mm(self):
        report = controller.verify_program(read_sample())
        self.assertTrue(report["ok"], report["violations"])
        self.assertTrue(report["units_injected"])
        self.assertEqual(report["structure"]["adapter_key"], controller.ADAPTER_KEY)
        self.assertEqual(report["structure"]["tool"], 2)
        self.assertTrue(report["structure"]["m141"])

    def test_unitless_source_is_rejected_without_the_setting(self):
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS=""):
            report = controller.verify_program(read_sample())
        self.assertFalse(report["ok"])
        self.assertTrue(any("واحد" in item for item in report["violations"]))

    def test_r_format_arc_is_rejected(self):
        text = "\r\n".join([
            "%", "G90G21G54", "G53Z0", "T2M6", "M141", "M6T2", "G43H2",
            "G0X0Y0Z5.000", "G1Z-2.000F9000.0",
            "G1Y200.000", "G1X150.000", "G1Y0.000", "G1X0.000",
            "G2X50Y50R50.000",
            "G0Z5.000", "G0X0Y0", "G53Z0", "M30", "%",
        ]) + "\r\n"
        report = controller.verify_program(text)
        self.assertFalse(report["ok"])
        self.assertTrue(any("R" in item for item in report["violations"]), report["violations"])

    def test_missing_machine_park_is_rejected(self):
        text = "\r\n".join([
            "%", "G90G21G54", "G53Z0", "T2M6", "M141", "M6T2", "G43H2",
            "G0X0Y0Z5.000", "G1Z-2.000F9000.0",
            "G1Y200.000", "G1X150.000", "G1Y0.000", "G1X0.000",
            "G0Z5.000", "G0X0Y0", "M30", "%",
        ]) + "\r\n"
        report = controller.verify_program(text)
        self.assertFalse(report["ok"])

    def test_empty_text_is_rejected(self):
        self.assertFalse(controller.verify_program("   ")["ok"])


@override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm", CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router")
class PostprocessResizeTests(SimpleTestCase):
    def test_identity_delta_keeps_program_and_injects_units(self):
        text = read_sample()
        result = core.process(text, {"mode": "equal", "delta": 0})
        output = controller.postprocess_resize(text, result["cnc_text"])
        self.assertIn("G90G21G54", output["text"])
        self.assertEqual(output["tool"], 2)
        self.assertTrue(output["units_injected"])

    def test_resized_program_verifies_and_keeps_line_structure(self):
        text = read_sample()
        result = core.process(text, {"mode": "equal", "delta": 60})
        output = controller.postprocess_resize(text, result["cnc_text"])
        self.assertEqual(len(output["text"].splitlines()), len(text.splitlines()))
        self.assertTrue(output["text"].startswith("%"))

    def test_out_of_dialect_candidate_is_blocked(self):
        text = read_sample()
        mutated = text.replace("N2280G53Z0\r\n", "").replace("N2290G53X0Y3950\r\n", "")
        mutated = mutated.replace("N2280G53Z0\n", "").replace("N2290G53X0Y3950\n", "")
        self.assertNotIn("G53X0Y3950", mutated)
        with self.assertRaises(controller.ControllerPostprocessError):
            controller.postprocess_resize(text, mutated)

    def test_source_outside_dialect_raises_with_reasons(self):
        tapered = "%\nG90\nG0 X0 Y0\nG1 X200 Y0\nG1 X250 Y300\nG1 X100 Y400\nG1 X30 Y200\nG1 X0 Y0\nG0 X0 Y0\nM30\n%\n"
        with self.assertRaises(controller.ControllerPostprocessError):
            controller.postprocess_resize(tapered, tapered)


class NormalizeUnitsTests(SimpleTestCase):
    def test_no_injection_when_g21_present(self):
        text = "%\r\nG90G21G54\r\n%\r\n"
        out, injected = controller.normalize_units(text)
        self.assertFalse(injected)
        self.assertEqual(out, text)

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="")
    def test_no_injection_when_setting_off(self):
        text = "%\nN10G90G54\nN70G0X1Y2\n%\n"
        out, injected = controller.normalize_units(text)
        self.assertFalse(injected)
        self.assertNotIn("G21", out)

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm")
    def test_injection_into_g90_block(self):
        text = "%\nN10G90G54\nN70G0X1Y2\n%\n"
        out, injected = controller.normalize_units(text)
        self.assertTrue(injected)
        self.assertIn("N10G90G21G54", out)
        self.assertEqual(len(out.splitlines()), len(text.splitlines()))


class AnalyzeSummaryTests(SimpleTestCase):
    def test_analyze_returns_file_summary_and_block_metadata(self):
        result = core.analyze(read_sample())
        self.assertTrue(result["ok"])
        summary = result["summary"]
        self.assertEqual(summary["units"], None)          # sample carries no G20/G21
        self.assertIn(90, summary["g_codes"])
        self.assertIn(141, summary["m_codes"])
        self.assertEqual(summary["tools"], [2])
        self.assertEqual(summary["arc_format"], "IJ")
        self.assertEqual(summary["n_numbering_step"], 10)
        self.assertTrue(summary["depth_levels"])          # negative cutting depths
        self.assertEqual(summary["work_offsets"], ["G54"])
        feature = result["features"][0]
        self.assertIsNotNone(feature["tool"])
        self.assertIsNotNone(feature["feed"])
