import math
import os
import unittest

from . import gcode_sheet as gs

SAMPLE = os.path.join(os.path.dirname(__file__), "sample.cnc")


def _read():
    with open(SAMPLE, encoding="utf-8-sig") as stream:
        return stream.read()


def _arcs(body):
    """Yield (start, end, centre_offset, mode) for every arc line."""
    cur = [None, None]
    mode = None
    for line in body:
        if "G53" in line:
            continue
        words = gs._words(line)
        vals = {}
        for letter, value, _ in words:
            if letter == "G" and value in (0, 1, 2, 3):
                mode = int(value)
            if letter in "XYIJ":
                vals[letter] = value
        end = [vals.get("X", cur[0]), vals.get("Y", cur[1])]
        if mode in (2, 3) and cur[0] is not None and ("I" in vals or "J" in vals):
            yield tuple(cur), tuple(end), (vals.get("I", 0.0), vals.get("J", 0.0)), mode
        cur = end


class GcodeSheetTests(unittest.TestCase):
    def setUp(self):
        self.pre, self.body, self.post = gs.split_program(_read())
        self.bounds = gs.source_bounds(self.body)

    def test_split_program_keeps_machine_park_out_of_body(self):
        self.assertTrue(any("M6" in l for l in self.pre))
        self.assertTrue(all("G53" not in l for l in self.body))
        self.assertEqual([l.replace(" ", "") for l in self.post], ["G53Z0", "G53X0Y3950"])

    def test_bounds_ignore_rapid_park_move_and_include_arcs(self):
        min_x, min_y, max_x, max_y = self.bounds
        self.assertAlmostEqual(min_x, -0.5, delta=0.6)
        self.assertGreater(max_y, 1700)
        self.assertLess(max_y, 1800)  # not inflated to the 3950 park position

    def test_translation_moves_bounding_box_to_placement(self):
        moved = gs.transform_body(self.body, self.bounds, 105, 205)
        min_x, min_y, max_x, max_y = gs.source_bounds(moved)
        self.assertAlmostEqual(min_x, 105, places=2)
        self.assertAlmostEqual(min_y, 205, places=2)
        self.assertAlmostEqual(max_x - min_x, self.bounds[2] - self.bounds[0], places=2)
        self.assertAlmostEqual(max_y - min_y, self.bounds[3] - self.bounds[1], places=2)

    def test_rotation_swaps_footprint_and_keeps_arcs_consistent(self):
        width = self.bounds[2] - self.bounds[0]
        height = self.bounds[3] - self.bounds[1]
        moved = gs.transform_body(self.body, self.bounds, 50, 80, rotated=True)
        min_x, min_y, max_x, max_y = gs.source_bounds(moved)
        self.assertAlmostEqual(min_x, 50, places=2)
        self.assertAlmostEqual(min_y, 80, places=2)
        self.assertAlmostEqual(max_x - min_x, height, places=2)
        self.assertAlmostEqual(max_y - min_y, width, places=2)
        original = list(_arcs(self.body))
        rotated = list(_arcs(moved))
        self.assertEqual(len(original), len(rotated))
        self.assertGreater(len(original), 0)
        for (s0, e0, c0, m0), (s1, e1, c1, m1) in zip(original, rotated):
            self.assertEqual(m0, m1)  # rotation keeps G2/G3
            r_start0 = math.hypot(c0[0], c0[1])
            r_end0 = math.hypot(e0[0] - (s0[0] + c0[0]), e0[1] - (s0[1] + c0[1]))
            r_start1 = math.hypot(c1[0], c1[1])
            r_end1 = math.hypot(e1[0] - (s1[0] + c1[0]), e1[1] - (s1[1] + c1[1]))
            self.assertAlmostEqual(r_start0, r_start1, places=2)
            self.assertAlmostEqual(r_end0, r_end1, places=2)

    def test_build_sheet_program_has_single_header_and_park(self):
        text = _read()
        program = gs.build_sheet_program("test", [
            {"text": text, "place_x": 5, "place_y": 5, "rotated": False, "name": "A"},
            {"text": text, "place_x": 500, "place_y": 5, "rotated": True, "name": "B"},
        ])
        self.assertEqual(program.count("M6"), 2)  # N30T2M6 and N50 M6T2 - once, from the first preamble
        self.assertEqual(program.count("G53X0Y3950"), 1)
        self.assertEqual(program.count("M30"), 1)
        self.assertEqual(program.count("; PART"), 2)
        self.assertTrue(program.startswith("%"))
        self.assertTrue(program.rstrip().endswith("%"))

    def test_unparseable_motion_line_is_rejected(self):
        with self.assertRaises(gs.GcodeError):
            gs.transform_body(["G1 X#1 Y5"], (0, 0, 10, 10), 0, 0)
