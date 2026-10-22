"""Django-free regression tests for the confirmed core.py bugs of the
archive review (prompt-1, section 2): comment parsing (#4), ramp/rapid depth
tracking (#5), finished-size tool compensation (#8) and the zero-shift line
rewrite (#9). Runs with plain ``python -m unittest``.
"""
import unittest

from . import core


TWO_ZONE = "\n".join([
    "%",
    "N10G90G54",
    "N20G53 Z0",
    "N30T3M6",
    "N40 M141",
    "N50 M6T3",
    "N60 G43 H3",
    "N70G0X10.000Y10.000Z5.000M3 S16000",
    "N80G1Z-14.000F10000.0",
    "N90G1X290.000",
    "N100G1Y290.000",
    "N110G1X10.000",
    "N120G1Y10.000",
    "N130G0Z5.000M3 S16000",
    "N140G0X10.000Y500.000M3 S16000",
    "N150G1Z-14.000F10000.0",
    "N160G1X290.000",
    "N170G1Y780.000",
    "N180G1X10.000",
    "N190G1Y500.000",
    "N200G0Z5.000M3 S16000",
    "N210G0X0.000Y0.000M3 S16000",
    "N220G53Z0",
    "N230G53X0Y3950",
    "N240M30",
    "%",
]) + "\n"


class CommentParsingTests(unittest.TestCase):
    """Bug #4: get_val and apply_resize must ignore (...)/; comments."""

    def test_get_val_ignores_trailing_comment(self):
        self.assertEqual(core.get_val("G1 X10 (see Y5.5)", "Y"), None)
        self.assertEqual(core.get_val("G1 X10 (see Y5.5)", "X"), 10.0)

    def test_get_val_ignores_leading_comment(self):
        self.assertEqual(core.get_val("(T9 tool) G1 X1 Y2", "T"), None)
        self.assertEqual(core.get_val("(T9 tool) G1 X1 Y2", "X"), 1.0)

    def test_get_val_ignores_semicolon_comment(self):
        self.assertEqual(core.get_val("G1 X1 Y2 ; T7 cleanup", "T"), None)
        self.assertEqual(core.get_val("G1 X1 Y2 ; Y9 drift", "Y"), 2.0)

    def test_blocks_tool_not_taken_from_comment(self):
        blocks = core._blocks(["G0 X0 Y0", "(T9 tool) G1 X1 Y2"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["tool"], None)

    def test_apply_resize_does_not_patch_comment_y(self):
        lines = ["(drill Y5.5 first)", "G1 X10 Y20"]
        out, _ = core.apply_resize(lines, lambda y: 100.0)
        self.assertEqual(out[0], "(drill Y5.5 first)")       # comment untouched
        self.assertEqual(out[1], "G1 X10 Y120.000")          # real word patched

    def test_detect_geometry_with_commented_coordinates(self):
        lines = [
            "G90",
            "G0 X0 Y0",
            "G1 Z-2",
            "(rapid to Y5.5 first) G1 X0 Y300",
            "G1 X200 Y300",
            "G1 X200 Y0",
            "G1 X0 Y0",
        ]
        levels, lo, hi = core.detect_geometry(lines)
        self.assertEqual(lo, 0.0)
        self.assertEqual(hi, 300.0)


class DepthTrackingTests(unittest.TestCase):
    """Bug #5: block depth must follow the LAST cutting Z, including ramp
    entries and G0 Z re-approaches."""

    def test_ramp_entry_records_depth(self):
        lines = [
            "G90",
            "G0 X0 Y0 Z5",
            "G1 X10 Y0 Z-3",      # ramp: XY and Z together
            "G1 X20 Y0",
            "G0 Z5",
        ]
        blocks = core._blocks(lines)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["z"], -3.0)

    def test_g0_z_reapproach_then_cut_records_new_depth(self):
        lines = [
            "G90",
            "G0 X0 Y0 Z5",
            "G1 Z-14",
            "G1 X20 Y0",
            "G0 Z-16",            # rapid down to the second depth
            "G1 X40 Y0",          # cutting again, no Z word
            "G0 Z5",
        ]
        blocks = core._blocks(lines)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["z"], -16.0)

    def test_retract_does_not_overwrite_depth(self):
        lines = [
            "G90",
            "G0 X0 Y0 Z5",
            "G1 Z-14",
            "G1 X20 Y0",
            "G0 Z5",              # retract; nothing cut afterwards
        ]
        blocks = core._blocks(lines)
        self.assertEqual(blocks[0]["z"], -14.0)

    def test_analyze_program_reports_real_depths(self):
        inventory = core.analyze_program([
            "G90",
            "G0 X0 Y0 Z5",
            "G1 X10 Y0 Z-3",
            "G1 X20 Y0",
        ])
        self.assertEqual(inventory[0]["depth"], -3.0)


class ZeroShiftIdentityTests(unittest.TestCase):
    """Bug #9: lines whose shift is zero must stay byte-identical."""

    def test_apply_resize_leaves_zero_shift_lines_untouched(self):
        lines = ["G0X0Y0Z5", "G1 X10 Y20.5"]
        out, _ = core.apply_resize(lines, lambda y: 0.0)
        self.assertEqual(out, lines)

    def test_changed_lines_excludes_zero_shift_lines(self):
        result = core.process(TWO_ZONE, {
            "mode": "manual",
            "manual": [50.0],                # the single detected gap grows
            "tool_dia": 8.0,
            "tool_map": {"3": 8.0},
        })
        self.assertTrue(result["resizable"])
        # features = [500, 780] (the last large block wins the outline vote):
        # only Y > 640 moves, every other line stays byte-identical
        lines = core.split_lines(TWO_ZONE)[0]
        self.assertEqual(set(result["changed_lines"]), {17})
        self.assertIn("Y830.000", lines[17].replace("Y780.000", "Y830.000"))
        for index in (7, 10, 12, 14, 19, 21):   # zero-shift Y-bearing lines
            self.assertNotIn(index, result["changed_lines"])
        self.assertEqual(lines[14], core.split_lines(result["cnc_text"])[0][14])

    def test_zero_delta_is_byte_identity_on_workshop_dialect(self):
        result = core.process(TWO_ZONE, {"mode": "equal", "delta": 0, "tool_dia": 8.0})
        self.assertEqual(result["cnc_text"], TWO_ZONE)
        self.assertEqual(result["changed_lines"], [])


class FinishedLengthTests(unittest.TestCase):
    """Bug #8: finished size = tool-centre path + tool diameter."""

    def test_finished_length_compensates_tool_table_diameter(self):
        result = core.process(TWO_ZONE, {
            "mode": "equal", "delta": 0, "tool_dia": 8.0, "tool_map": {"3": 8.0},
        })
        self.assertAlmostEqual(
            result["finished_length_before"],
            result["length_before"] + 8.0, places=2)
        self.assertAlmostEqual(
            result["finished_length_after"],
            result["finished_length_before"] + result["delta"], places=2)
        self.assertEqual(result["finished_tool_dia"], 8.0)

    def test_finished_length_falls_back_to_request_diameter(self):
        result = core.process(TWO_ZONE, {"mode": "equal", "delta": 0, "tool_dia": 6.0})
        self.assertAlmostEqual(
            result["finished_length_before"],
            result["length_before"] + 6.0, places=2)

    def test_finished_length_tracks_the_resize_delta(self):
        result = core.process(TWO_ZONE, {
            "mode": "equal", "delta": 100.0, "tool_dia": 8.0, "tool_map": {"3": 8.0},
        })
        self.assertAlmostEqual(
            result["finished_length_after"] - result["finished_length_before"],
            100.0, places=2)

    def test_view_only_result_carries_finished_fields(self):
        # a circle-like file: no long straight Y edges -> view-only
        circle = "\n".join([
            "G21 G90 G17",
            "G0 X500.000 Y10.000 Z5.000",
            "G1 Z-16.000 F10000.0",
            "G2 X10.000 Y500.000 I0.000 J490.000",
            "G2 X500.000 Y990.000 I490.000 J0.000",
            "G2 X990.000 Y500.000 I0.000 J-490.000",
            "G2 X500.000 Y10.000 I-490.000 J0.000",
            "G0 Z5.000",
        ]) + "\n"
        result = core.process(circle, {"delta": 10, "allow_view_only": True, "tool_dia": 8.0})
        self.assertFalse(result["resizable"])
        self.assertIn("finished_length_before", result)
        self.assertAlmostEqual(result["finished_length_before"],
                               result["length_before"] + 8.0, places=2)


if __name__ == "__main__":
    unittest.main()
