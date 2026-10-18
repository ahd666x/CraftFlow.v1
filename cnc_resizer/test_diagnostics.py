"""Regression tests for read-only stage-one CNC diagnostics."""
import hashlib
import json
import unittest
from pathlib import Path

from . import core
from .diagnostics import analyze_text

APP_DIR = Path(__file__).resolve().parent
SAMPLE = APP_DIR / "sample.cnc"
BASELINE = APP_DIR / "reports" / "sample_diagnostics.json"
TARGET_BASELINE = APP_DIR / "reports" / "t.16.1090x450.2set.baseline.json"


class DiagnosticReportTests(unittest.TestCase):
    def test_target_attachment_baseline_has_expected_separate_feature_candidates(self):
        report = json.loads(TARGET_BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(report["input"]["filename"], "t.16.1090x450.2set.cnc")
        self.assertFalse(report["input"]["source_text_modified"])
        self.assertFalse(report["input"]["source_program_included"])
        self.assertTrue(all(report["acceptance"].values()))
        self.assertEqual(sum(item["kind"] == "small_loop_candidate" for item in report["features"]), 4)
        self.assertEqual(sum(item["kind"] == "raster_or_zigzag_candidate" for item in report["features"]), 2)
        self.assertTrue(any(item["kind"] == "large_closed_path_candidate" for item in report["features"]))
        self.assertEqual(report["modal_and_commands"]["unknown_m_codes"][0]["code"], "M141")
        self.assertFalse(report["safety"]["machine_output_written"])

    def test_checked_in_sample_matches_locked_baseline_report(self):
        raw = SAMPLE.read_bytes()
        text = raw.decode("utf-8-sig")
        actual = analyze_text(
            text,
            filename="cnc_resizer/sample.cnc",
            source_sha256=hashlib.sha256(raw).hexdigest(),
        )
        expected = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(actual, expected)

    def test_report_does_not_modify_source_and_never_authorizes_machine_output(self):
        before = SAMPLE.read_bytes()
        report = analyze_text(before.decode("utf-8-sig"), filename=SAMPLE.name)
        self.assertEqual(SAMPLE.read_bytes(), before)
        self.assertFalse(report["input"]["content_modified"])
        self.assertFalse(report["safety"]["machine_output_written"])
        self.assertFalse(report["safety"]["nonzero_resize_executed"])
        self.assertFalse(report["safety"]["safe_for_execution"])
        self.assertFalse(report["safety"]["controller_identified"])

    def test_unknown_m141_and_compact_m6t3_are_reported_with_line_numbers(self):
        text = "N10G90G21G17\nN20T3M6\nN30G0X0Y0Z5\nN40M141\nN50M6T3\n"
        report = analyze_text(text, filename="unknown-controller.cnc")
        self.assertEqual(report["commands"]["unknown_m_codes"], [
            {"token": "M141", "source_lines": [4]},
        ])
        self.assertEqual(report["commands"]["compact_m_t_tokens"], [
            {"token": "M6T3", "source_lines": [5]},
        ])
        self.assertEqual(report["program_summary"]["tools"][0]["number"], 3)

    def test_small_closed_arc_path_is_only_a_hole_candidate_not_a_claim(self):
        text = "\n".join([
            "G21 G90 G17", "T3 M6", "G0 X2 Y0 Z5", "G1 Z-7",
            "G3 X0 Y2 I-2 J0", "G3 X-2 Y0 I0 J-2",
            "G3 X0 Y-2 I2 J0", "G3 X2 Y0 I0 J2", "M30", "",
        ])
        report = analyze_text(text)
        block = next(block for block in report["blocks"] if block["segment_count"] > 0)
        self.assertEqual(block["classification"]["candidate"], "small_closed_loop_or_hole")
        self.assertEqual(block["classification"]["confidence"], "high_geometry_only")
        self.assertTrue(block["closed_by_endpoints"])
        arc = next(move["arc"] for move in block["moves"] if "arc" in move)
        self.assertEqual(arc["validity"], "radius_consistent")

    def test_alternating_long_passes_are_separated_from_contour_candidates(self):
        text = "\n".join([
            "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-2",
            "G1 X500 Y0", "G1 X0 Y14", "G1 X500 Y14", "G1 X0 Y28",
            "G1 X500 Y28", "G1 X0 Y42", "G1 X500 Y42", "M30", "",
        ])
        report = analyze_text(text)
        block = next(block for block in report["blocks"] if block["segment_count"] > 0)
        self.assertEqual(block["classification"]["candidate"], "zigzag_or_raster_path")
        self.assertFalse(block["closed_by_endpoints"])

    def test_legacy_detector_overwrites_first_contour_with_last_large_block(self):
        # This fixture deliberately records the current bug: the second large
        # path is a raster-like region, but legacy detect_geometry treats it as
        # the main outline because it overwrites the last qualifying block.
        text = "\n".join([
            "G90",
            "G0 X0 Y0", "G1 X0 Y200", "G1 X200 Y200", "G1 X200 Y0", "G1 X0 Y0",
            "G0 X300 Y300", "G1 X300 Y500", "G1 X500 Y500", "G1 X500 Y300", "G1 X300 Y300",
            "M30", "",
        ])
        _, edge_low, edge_high = core.detect_geometry(text.splitlines())
        self.assertEqual((edge_low, edge_high), (300.0, 500.0))
        report = analyze_text(text)
        self.assertEqual(len(report["legacy_detector_candidates"]["large_blocks"]), 2)
        self.assertEqual(report["legacy_detector_candidates"]["last_large_block_would_be_selected"], "block-0002")
        self.assertTrue(any("آخرین بلوک" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
