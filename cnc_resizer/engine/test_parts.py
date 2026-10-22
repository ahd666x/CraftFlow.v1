"""Django-free coverage for engine.parts (part-level detection).

Runs with plain ``python -m unittest cnc_resizer.engine.test_parts`` — no
Django, no settings, no database. The flagship acceptance file ships in
``cnc_resizer/testdata/``; the full 65-file archive table only runs when the
extracted archive directory exists (skipped otherwise, like sample.cnc).
"""
import math
import os
import re
import unittest

from .parser import parse_program
from .parts import (
    DEFAULT_MIN_PART_SIZE,
    _band_intervals,
    _merge_paths,
    _extract_loops,
    classify_shape,
    detect_parts,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata")
FLAGSHIP = os.path.join(DATA_DIR, "t.1.1970x395.2set.cnc")
ARCHIVE_DIR = os.environ.get("GCODE_ARCHIVE_DIR", "/home/z/my-project/gcode_full")


def _rect(x0, y0, x1, y1, depth=-16.0, tool=3):
    """A closed rectangular contour block (CCW) at one depth."""
    lines = [
        "G21 G90 G17",
        f"G0 X{x0:.3f} Y{y0:.3f} Z5.000",
        f"G1 Z{depth:.3f} F10000.0",
        f"G1 X{x1:.3f} Y{y0:.3f}",
        f"G1 X{x1:.3f} Y{y1:.3f}",
        f"G1 X{x0:.3f} Y{y1:.3f}",
        f"G1 X{x0:.3f} Y{y0:.3f}",
        "G0 Z5.000",
    ]
    return "\n".join(lines) + "\n"


class SyntheticDetectionTests(unittest.TestCase):
    def _detect(self, text, params=None, filename="synthetic.cnc"):
        return detect_parts(parse_program(text, filename=filename), params)

    def test_two_stacked_parts_are_two_parts(self):
        text = _rect(0, 0, 200, 300) + _rect(0, 400, 200, 700)
        result = self._detect(text)
        self.assertEqual(len(result["parts"]), 2)
        widths = sorted(round(part["bbox"]["height"], 3) for part in result["parts"])
        self.assertEqual(widths, [300.0, 300.0])
        self.assertEqual(result["sheet_bbox"]["height"], 700.0)

    def test_depth_passes_merge_into_one_part(self):
        text = (_rect(0, 0, 200, 300, depth=-14.0)
                + _rect(0, 0, 200, 300, depth=-16.0))
        result = self._detect(text)
        self.assertEqual(len(result["parts"]), 1)
        part = result["parts"][0]
        # passes stay in source-line order (the -14 pass is cut first)
        self.assertEqual(sorted(item["z"] for item in part["passes"]), [-16.0, -14.0])
        self.assertEqual(sorted(part["depths"]), [-16.0, -14.0])

    def test_hole_inside_becomes_child_not_part(self):
        text = _rect(0, 0, 400, 400) + _rect(100, 100, 160, 160, depth=-16.0)
        result = self._detect(text)
        self.assertEqual(len(result["parts"]), 1)
        self.assertEqual(len(result["parts"][0]["children"]), 1)
        self.assertEqual(result["parts"][0]["children"][0]["kind"], "pocket_or_hole")

    def test_circle_is_recognized_without_straight_edges(self):
        # j.26-style round part: four G2 quarter arcs around (500,500), r=490,
        # no straight edge at all
        lines = [
            "G21 G90 G17",
            "G0 X500.000 Y10.000 Z5.000",
            "G1 Z-16.000 F10000.0",
            "G2 X10.000 Y500.000 I0.000 J490.000",
            "G2 X500.000 Y990.000 I490.000 J0.000",
            "G2 X990.000 Y500.000 I0.000 J-490.000",
            "G2 X500.000 Y10.000 I-490.000 J0.000",
            "G0 Z5.000",
        ]
        result = self._detect("\n".join(lines) + "\n")
        self.assertEqual(len(result["parts"]), 1)
        part = result["parts"][0]
        self.assertEqual(part["shape_kind"], "circle")
        self.assertFalse(part["capabilities"]["can_stretch_y"])
        self.assertFalse(part["capabilities"]["can_stretch_x"])
        self.assertTrue(part["capabilities"]["can_translate"])
        self.assertIsNone(part["stretch_band_y"])

    def test_open_saw_line_becomes_operation(self):
        lines = [
            "G21 G90 G17",
            "G0 X380.000 Y50.000 Z5.000",
            "G1 Z-19.000 F10000.0",
            "G1 Y2300.000",
            "G0 Z5.000",
        ]
        result = self._detect("\n".join(lines) + "\n")
        self.assertEqual(result["parts"], [])
        self.assertEqual(len(result["operations"]), 1)
        self.assertEqual(result["operations"][0]["kind"], "saw_line")

    def test_raster_becomes_raster_operation(self):
        moves = []
        y = 10.0
        for index in range(6):
            x_from = 10.0 if index % 2 == 0 else 100.0
            x_to = 100.0 if index % 2 == 0 else 10.0
            moves.append(f"G1 X{x_from:.3f} Y{y:.3f}")
            moves.append(f"G1 X{x_to:.3f} Y{y:.3f}")
            y += 3.0
        lines = ["G21 G90 G17", "G0 X10.000 Y10.000 Z5.000", "G1 Z-2.000 F10000.0"] + moves
        result = self._detect("\n".join(lines) + "\n")
        self.assertEqual(result["parts"], [])
        self.assertEqual([op["kind"] for op in result["operations"]], ["raster"])

    def test_shallow_open_path_is_engraving(self):
        # a full-depth rect establishes the sheet thickness; the shallow line
        # inside it is an engraving operation of that part
        lines = [
            "G21 G90 G17",
            "G0 X0.000 Y0.000 Z5.000",
            "G1 Z-16.000 F10000.0",
            "G1 X400.000 Y0.000",
            "G1 X400.000 Y300.000",
            "G1 X0.000 Y300.000",
            "G1 X0.000 Y0.000",
            "G0 Z5.000",
            "G0 X50.000 Y50.000 Z5.000",
            "G1 Z-1.000 F10000.0",
            "G1 X50.000 Y250.000",
            "G0 Z5.000",
        ]
        result = self._detect("\n".join(lines) + "\n")
        self.assertEqual(len(result["parts"]), 1)
        self.assertEqual(len(result["operations"]), 1)
        self.assertEqual(result["operations"][0]["kind"], "engraving")
        self.assertEqual(result["operations"][0]["assigned_part"], result["parts"][0]["id"])

    def test_min_part_size_is_configurable(self):
        default = self._detect(_rect(0, 0, 50, 50))
        self.assertEqual(len(default["parts"]), 1)  # 50x50 passes the 15mm default
        tiny = self._detect(_rect(0, 0, 14, 14))
        self.assertEqual(tiny["parts"], [])
        relaxed = self._detect(_rect(0, 0, 14, 14), {"min_part_size": 10.0})
        self.assertEqual(len(relaxed["parts"]), 1)

    def test_tapered_part_has_band_but_not_full_height(self):
        lines = [
            "G21 G90 G17",
            "G0 X0.000 Y0.000 Z5.000",
            "G1 Z-16.000 F10000.0",
            "G1 X0.000 Y400.000",
            "G1 X200.000 Y400.000",
            "G1 X200.000 Y60.000",
            "G1 X120.000 Y0.000",     # slanted corner edge
            "G1 X0.000 Y0.000",
            "G0 Z5.000",
        ]
        result = self._detect("\n".join(lines) + "\n")
        part = result["parts"][0]
        self.assertEqual(part["shape_kind"], "tapered")
        band = part["stretch_band_y"]
        self.assertIsNotNone(band)
        self.assertGreaterEqual(band[0], 59.0)   # above the slanted zone
        self.assertGreater(band[1], 100.0)

    def test_band_intervals_exclude_arcs(self):
        lines = [
            "G21 G90 G17",
            "G0 X0.000 Y0.000 Z5.000",
            "G1 Z-16.000 F10000.0",
            "G1 X100.000 Y0.000",
            "G2 X120.000 Y20.000 I0.000 J20.000",
            "G1 X120.000 Y300.000",
            "G2 X100.000 Y320.000 I-20.000 J0.000",
            "G1 X0.000 Y320.000",
            "G1 X0.000 Y0.000",
            "G0 Z5.000",
        ]
        program = parse_program("\n".join(lines) + "\n")
        loops = _extract_loops(program)
        paths = _merge_paths(loops, 0.6)
        canonical = paths[0].canonical
        bands = _band_intervals(canonical, "y", 0.6)
        self.assertTrue(bands)
        low, high = max(bands, key=lambda item: item[1] - item[0])
        self.assertGreaterEqual(low, 19.9)   # above the bottom arc zone
        self.assertLessEqual(high, 300.1)    # below the top arc zone

    def test_crossing_paths_are_locked_parts_not_silent_ops(self):
        # two "+"-crossing rectangles: both stay visible as parts per the
        # review contract (warn + lock editing), never silently swallowed
        text = _rect(0, 100, 400, 200) + _rect(150, 0, 250, 300)
        result = self._detect(text)
        self.assertEqual(len(result["parts"]), 2)
        self.assertTrue(all(part["locked"] for part in result["parts"]))
        self.assertFalse(all(part["capabilities"]["can_translate"] for part in result["parts"]))

    def test_stable_ids_do_not_depend_on_block_order(self):
        one = _rect(0, 0, 200, 300) + _rect(0, 400, 200, 700)
        two = _rect(0, 400, 200, 700) + _rect(0, 0, 200, 300)
        ids_one = [part["id"] for part in self._detect(one)["parts"]]
        ids_two = [part["id"] for part in self._detect(two)["parts"]]
        self.assertEqual(sorted(ids_one), sorted(ids_two))
        self.assertNotEqual(ids_one[0], ids_one[1])

    def test_filename_cross_check_warns_only(self):
        text = _rect(0, 0, 200, 300, tool=3)
        result = self._detect(text, filename="panel.999x999.1set.cnc")
        self.assertTrue(any("نمی‌خواند" in warning for warning in result["warnings"]))
        ok = self._detect(text, filename="panel.208x308.1set.cnc")
        self.assertFalse(any("نمی‌خواند" in warning for warning in ok["warnings"]))

    def test_bbox_finished_compensates_tool_diameter(self):
        text = _rect(0, 0, 200, 300, tool=3)
        result = self._detect(text, {"tool_map": {"3": 8.0}})
        part = result["parts"][0]
        self.assertEqual(part["tool_dia_used"], 8.0)
        self.assertAlmostEqual(part["bbox_finished"]["width"], 208.0, places=3)
        self.assertAlmostEqual(part["bbox_finished"]["height"], 308.0, places=3)
        fallback = self._detect(text, {"tool_dia": 6.0})
        self.assertAlmostEqual(fallback["parts"][0]["bbox_finished"]["width"], 206.0, places=3)

    def test_detection_never_raises_on_garbage_geometry(self):
        lines = ["G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-1", "G1 X10 Y3", "G0 Z5"]
        result = self._detect("\n".join(lines) + "\n")
        self.assertIn("parts", result)
        self.assertIn("operations", result)


class ParametricPartEditTests(unittest.TestCase):
    @staticmethod
    def _three_panels_with_holes():
        text = ""
        for low in (0.0, 120.0, 240.0):
            text += _rect(0, low, 100, low + 100)
            text += _rect(20, low + 20, 35, low + 30)
        return text

    def test_chain_stretch_applies_cumulative_offsets_and_lower_anchors_children(self):
        from .part_edits import edits_part_program
        text = self._three_panels_with_holes()
        parts = sorted(detect_parts(parse_program(text))["parts"],
                       key=lambda part: part["bbox"]["y_min"])
        result = edits_part_program(text, {"edits": [{
            "op": "stretch_chain", "axis": "y",
            "parts": [part["id"] for part in parts], "delta": 10.0,
        }]})
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        resized = sorted(result["resized"]["parts"], key=lambda part: part["bbox"]["y_min"])
        for rank, (before, after) in enumerate(zip(parts, resized)):
            self.assertAlmostEqual(after["bbox"]["y_min"], before["bbox"]["y_min"] + 10 * rank)
            self.assertAlmostEqual(after["bbox"]["y_max"], before["bbox"]["y_max"] + 10 * (rank + 1))
            self.assertEqual(len(after["children"]), 1)
            self.assertAlmostEqual(after["children"][0]["bbox"]["y_min"],
                                   before["children"][0]["bbox"]["y_min"] + 10 * rank)

    def test_single_part_stretch_keeps_child_distance_from_lower_edge(self):
        from .part_edits import edits_part_program
        text = self._three_panels_with_holes()
        part = sorted(detect_parts(parse_program(text))["parts"],
                      key=lambda item: item["bbox"]["y_min"])[0]
        result = edits_part_program(text, {"edits": [{
            "op": "stretch_part", "part_id": part["id"], "axis": "y", "delta": 10.0,
        }]})
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        resized = next(item for item in result["resized"]["parts"]
                       if item["contour_block_ids"] == part["contour_block_ids"])
        self.assertAlmostEqual(resized["children"][0]["bbox"]["y_min"],
                               part["children"][0]["bbox"]["y_min"])

    def test_translation_materializes_missing_absolute_axis_word(self):
        from .part_edits import edits_part_program
        from .serializer import serialize_candidate
        text = "\n".join([
            "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-3 F1000",
            "G1 X100", "G1 Y100", "G1 X0", "G1 Y0", "G0 Z5", "",
        ])
        program = parse_program(text)
        part = detect_parts(program)["parts"][0]
        result = edits_part_program(text, {"edits": [{
            "op": "move_part", "part_id": part["id"], "dx": 10.0, "dy": 0.0,
        }]})
        serialized = serialize_candidate(text, {
            "validation": result["validation"], "source_sha256": program.source_sha256,
            "program_ir": result["program_ir"],
        })
        self.assertIn("G1 Y100X110.000", serialized["text"])
        reparsed = parse_program(serialized["text"])
        self.assertEqual(len(reparsed.moves), len(program.moves))

    def test_duplicate_axis_word_is_still_blocked_as_a_real_ambiguity(self):
        from .ir import to_dict
        from .serializer import SerializerBlocked, serialize_candidate
        text = "\n".join([
            "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-3 F1000",
            "G1 X100 X90", "G1 Y100", "G1 X0", "G1 Y0", "G0 Z5", "",
        ])
        program = parse_program(text)
        candidate = to_dict(program)
        move = next(item for item in candidate["moves"]
                    if item["raw_line"].strip() == "G1 X100 X90")
        move["end"]["x"] += 1.0
        with self.assertRaises(SerializerBlocked) as ctx:
            serialize_candidate(text, {
                "validation": {"passed": True, "errors": []},
                "source_sha256": program.source_sha256, "program_ir": candidate,
            })
        self.assertIn("expected one X word, found 2", str(ctx.exception))


@unittest.skipUnless(os.path.exists(FLAGSHIP), "flagship fixture not shipped")
class FlagshipAcceptanceTests(unittest.TestCase):
    """Acceptance criterion 1 of the archive review, on the real workshop file."""

    @classmethod
    def setUpClass(cls):
        with open(FLAGSHIP, errors="replace") as handle:
            cls.text = handle.read()
        cls.program = parse_program(cls.text, filename="t.1.1970x395.2set.cnc")
        cls.result = detect_parts(cls.program)

    def test_exactly_three_parts_two_passes_each(self):
        self.assertEqual(len(self.result["parts"]), 3)
        for part in self.result["parts"]:
            self.assertEqual(len(part["passes"]), 2)
            self.assertEqual(sorted(item["z"] for item in part["passes"]), [-16.0, -14.0])

    def test_geometry_matches_the_manual_measurement(self):
        by_height = sorted(self.result["parts"], key=lambda part: part["bbox"]["y_min"])
        ends, middle = [by_height[0], by_height[2]], by_height[1]
        for part in ends:
            self.assertAlmostEqual(part["bbox"]["width"], 385.0, places=1)
            self.assertAlmostEqual(part["bbox"]["height"], 629.8, places=1)
            self.assertEqual(part["shape_kind"], "tapered")
        self.assertAlmostEqual(middle["bbox"]["width"], 249.0, places=1)
        self.assertAlmostEqual(middle["bbox"]["height"], 701.0, places=1)
        self.assertEqual(middle["shape_kind"], "rect_rounded")
        sheet = self.result["sheet_bbox"]
        self.assertAlmostEqual(sheet["width"], 385.0, places=1)
        self.assertAlmostEqual(sheet["height"], 1960.7, places=1)

    def test_stretch_middle_part_by_100(self):
        from ..engine.part_edits import edits_part_program
        middle = sorted(self.result["parts"], key=lambda part: part["bbox"]["y_min"])[1]
        result = edits_part_program(self.text, {"edits": [
            {"op": "stretch_part", "part_id": middle["id"], "axis": "y", "delta": 100.0},
        ]}, filename="t.1.1970x395.2set.cnc")
        self.assertTrue(result["validation"]["passed"],
                        msg=str(result["validation"]["errors"]))
        self.assertFalse(result["collision_check"]["flagged"])
        runtime_parts = result["resized"]["parts"]
        self.assertEqual(len(runtime_parts), 3)
        heights = sorted(part["bbox"]["height"] for part in runtime_parts)
        self.assertAlmostEqual(heights[2], 701.0 + 100.0, places=1)  # middle grew
        self.assertAlmostEqual(heights[0], 629.8, places=1)          # A rigid
        self.assertAlmostEqual(heights[1], 629.8, places=1)          # B rigid
        ys = sorted((part["bbox"]["y_min"], part["bbox"]["y_max"]) for part in runtime_parts)
        self.assertAlmostEqual(ys[0][0], 5.0, places=1)              # A stays
        self.assertAlmostEqual(ys[1][0], 634.9, places=1)            # middle bottom stays
        self.assertAlmostEqual(ys[2][0], 1336.0 + 100.0, places=1)   # B pushed up

    def test_stretched_output_passes_serializer_and_is_a_pure_patch(self):
        from ..engine.part_edits import edits_part_program
        from ..engine.serializer import serialize_candidate
        middle = sorted(self.result["parts"], key=lambda part: part["bbox"]["y_min"])[1]
        result = edits_part_program(self.text, {"edits": [
            {"op": "stretch_part", "part_id": middle["id"], "axis": "y", "delta": 100.0},
        ]}, filename="t.1.1970x395.2set.cnc")
        self.assertTrue(result["validation"]["passed"])
        draft = serialize_candidate(self.text, {
            "validation": {"passed": True, "errors": [], "warnings": []},
            "source_sha256": self.program.source_sha256,
            "program_ir": result["program_ir"],
        })
        source_lines = self.text.splitlines()
        out_lines = draft["text"].splitlines()
        self.assertEqual(len(source_lines), len(out_lines))          # 1:1 structure
        changed = [i for i, (a, b) in enumerate(zip(source_lines, out_lines)) if a != b]
        self.assertTrue(changed)
        for line_no in changed:
            source_word = source_lines[line_no].replace(" ", "")
            out_word = out_lines[line_no].replace(" ", "")
            # Only XY words may be added or rewritten on a patched line.
            strip_xy = lambda line: re.sub(
                r"(?<![A-Z])[XY][+-]?(?:\d+(?:\.\d*)?|\.\d+)", "", line, flags=re.I)
            self.assertEqual(strip_xy(source_word), strip_xy(out_word),
                             msg=f"line {line_no + 1} changed non-coordinate words")

    def test_stretch_rejects_arc_band_and_locked_parts(self):
        from ..engine.part_edits import edits_part_program
        ends = sorted(self.result["parts"], key=lambda part: part["bbox"]["y_min"])
        end_part = ends[0]
        # a band across the slanted edge must be rejected
        with self.assertRaises(Exception):
            edits_part_program(self.text, {"edits": [
                {"op": "stretch_part", "part_id": end_part["id"], "axis": "y",
                 "band": [5.0, 600.0], "delta": 50.0},
            ]}, filename="t.1.1970x395.2set.cnc")
        # valid explicit band inside the straight zone is accepted
        band = end_part["stretch_band_y"]
        ok = edits_part_program(self.text, {"edits": [
            {"op": "stretch_part", "part_id": end_part["id"], "axis": "y",
             "band": [band[0], band[1]], "delta": 10.0},
        ]}, filename="t.1.1970x395.2set.cnc")
        self.assertTrue(ok["validation"]["passed"], msg=str(ok["validation"]["errors"]))


@unittest.skipUnless(os.path.isdir(ARCHIVE_DIR), "g-code archive not available")
class ArchiveTableTests(unittest.TestCase):
    """Section-1 table of the archive review: expected part counts per file."""

    EXPECTED = {
        "t.1.1970x395.2set.cnc": 3,
        "k.1.1160x360.1set.cnc": 2,
        "k.30.1280x570.cnc": 2,
        "k.30.1310x380.cnc": 8,
        "k.30.1950x750.cnc": 6,
        "k.30.2010x490.cnc": 3,
        "t.30.2200x410.cnc": 6,
        "t.19.570x395.4sets.cnc": 4,
        "t.31.2220x327.2set.cnc": 4,
        "t.1.625x379.2set.cnc": 2,
        "t.1.365x438.1R.cnc": 2,
        "t.1.438x365.R.cnc": 2,
        "t.1.438x365.l.cnc": 2,
        "t.30.770x430.cnc": 2,
        "t.30.940x300.cnc": 2,
        "t.19.610x460.2set.cnc": 2,
        "k.1.1840x1220.1set.cnc": 5,
        "k.1.2200x830.1set.cnc": 5,
        "t.2.1900x400.1set.cnc": 2,
        "t.16.560x470.1set.cnc": 3,
        "t.19.585x370.1set.cnc": 4,
        "t.1.430x230.1set.cnc": 2,
        "t.1.570x470.1set.cnc": 2,
        "t.19.450x395.1set.cnc": 2,
        "j.22.850x720.1set.cnc": 6,
        "j.22.950x880.1set.cnc": 6,
        "j.26.940x940.1set.cnc": 7,          # 6 strips + 1 envelope
        "j.26.1080x1080.1set.cnc": 1,        # round part
        "t.8.2350x380.2set.1.cnc": 0,        # operations only
        "t.8.2350x380.2set.2.cnc": 0,
        "t.8.396x380.2set.cnc": 0,
        "t.2.720X150.1set.cnc": 0,           # raster/engraving only
        "j.13L.350x320.6set.cnc": 0,
        "k.6.650x580.1set.cnc": 0,
        "t.6.484x350.2set.cnc": 0,
        "t.6.810x410.1set.cnc": 0,
    }

    def test_no_archive_file_ever_raises(self):
        failures = []
        for name in sorted(os.listdir(ARCHIVE_DIR)):
            if not name.lower().endswith(".cnc"):
                continue
            with open(os.path.join(ARCHIVE_DIR, name), errors="replace") as handle:
                text = handle.read()
            try:
                result = detect_parts(parse_program(text, filename=name))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: raised {type(exc).__name__}: {exc}")
                continue
            self.assertIn("parts", result)
            self.assertIn("operations", result)
        self.assertEqual(failures, [])

    def test_expected_part_counts_match_the_review_table(self):
        mismatches = []
        for name, expected in self.EXPECTED.items():
            path = os.path.join(ARCHIVE_DIR, name)
            if not os.path.exists(path):
                mismatches.append(f"{name}: file missing from the archive directory")
                continue
            with open(path, errors="replace") as handle:
                text = handle.read()
            result = detect_parts(parse_program(text, filename=name))
            actual = len(result["parts"])
            if actual != expected:
                mismatches.append(f"{name}: expected {expected} parts, detected {actual}")
        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
