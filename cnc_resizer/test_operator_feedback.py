"""Operator-feedback round on the production map editor:

* the feature-level collision gate verifies the RUNTIME cutting paths before
  condemning a bbox-level overlap — bbox-only overlaps (clear paths, the
  normal case on nested sheets and L-shaped parts) downgrade to a
  non-blocking warning that still travels in ``collision_check.flagged``;
  genuinely crossing paths stay a hard fail-closed error;
* ``/api/engine/resize/`` restores the old whole-file length-change flow
  (equal/proportional/kerf distribution) for the map workspace: the resized
  candidate text comes back clearly marked preview-only, never machine output.
"""
import json
import unittest

from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .engine.service import edits_program
from .test_kerf import build_kerf_file

# block-0001: an L path (left wall + top wall), bbox [0..100] x [0..100]
# block-0002: a small rect to its right, bbox [105..115] x [40..60] —
# the two bboxes are disjoint with a 5mm gap, so any pair state starts clean.
L_SHAPE_TEXT = "\n".join([
    "%",
    "N10G90G54",
    "N20G53 Z0",
    "N30T3M6",
    "N40 M141",
    "N50 M6T3",
    "N60 G43 H3",
    "N70G0X0.000Y0.000Z5.000M3 S16000",
    "N80G1Z-16.000F10000.0",
    "N90G1Y100.000",
    "N100G1X100.000",
    "N110G0X105.000Y40.000Z5.000M3 S16000",
    "N120G1Z-16.000F10000.0",
    "N130G1X115.000",
    "N140G1Y60.000",
    "N150G1X105.000",
    "N160G1Y40.000",
    "N170G0Z5.000M3 S16000",
    "N180G0X0.000Y0.000M3 S16000",
    "N190G53Z0",
    "N200G53X0Y3950",
    "N210M30",
    "%",
]) + "\n"


class GeometryVerifiedCollisionGateTests(SimpleTestCase):
    """bbox partial overlap with clear runtime paths warns; crossing rejects."""

    def test_bbox_only_overlap_is_downgraded_to_warning(self):
        # shift the rect 10mm left: its bbox overlaps the L bbox by 5mm, but
        # the rect lands INSIDE the L's open side — paths stay ~40mm apart.
        result = edits_program(L_SHAPE_TEXT, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0002", "dx": -10.0, "dy": 0.0},
        ]}, filename="l.cnc")
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        flagged = result["collision_check"]["flagged"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["severity"], "warning")
        self.assertGreater(flagged[0]["overlap_x"], 0)
        self.assertGreater(flagged[0]["path_min_distance"], 10.0)
        self.assertEqual(result["collision_check"]["geometry_cleared"], 1)
        joined = " ".join(result["validation"]["warnings"])
        self.assertIn("paths stay", joined)

    def test_crossing_paths_stay_a_hard_error(self):
        # same 10mm shift plus +55mm up: the rect's left edge crosses the L's
        # top wall at (95, 100) — a real path collision must still be rejected.
        result = edits_program(L_SHAPE_TEXT, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0002", "dx": -10.0, "dy": 55.0},
        ]}, filename="l.cnc")
        self.assertFalse(result["validation"]["passed"])
        flagged = result["collision_check"]["flagged"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["severity"], "error")
        self.assertIs(flagged[0]["path_intersection"], True)
        self.assertEqual(result["collision_check"]["geometry_cleared"], 0)
        self.assertNotIn("bbox level (", " ".join(result["validation"]["warnings"]))

    def test_clean_pair_produces_no_flag_and_no_warning(self):
        result = edits_program(L_SHAPE_TEXT, {"edits": [
            {"op": "shift", "feature_id": "feature-block-0002", "dx": 50.0, "dy": 0.0},
        ]}, filename="l.cnc")
        self.assertTrue(result["validation"]["passed"])
        self.assertEqual(result["collision_check"]["flagged"], [])
        self.assertEqual(result["collision_check"]["geometry_cleared"], 0)


class EngineResizeEndpointTests(TestCase):
    """The map workspace regains the old length-change flow, preview-only."""

    @classmethod
    def setUpTestData(cls):
        cls.operator = User.objects.create_user("cnc-op-resize", password="testpass")
        group, _created = Group.objects.get_or_create(name="2")
        cls.operator.groups.add(group)

    def setUp(self):
        self.client.login(username="cnc-op-resize", password="testpass")

    def test_kerf_mode_returns_candidate_text_preview_only(self):
        resp = self.client.post(reverse("cnc_resizer:engine_resize"),
                                data=json.dumps({"content": build_kerf_file(),
                                                 "filename": "kerf.cnc",
                                                 "params": {"mode": "kerf", "delta": 28}}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["resizable"])
        self.assertFalse(data["safe_for_execution"])
        self.assertTrue(data["preview_only"])
        self.assertAlmostEqual(data["length_after"] - data["length_before"], 28.0, places=1)
        self.assertTrue(data["cnc_text"].strip())

    def test_equal_mode_reports_lengths(self):
        resp = self.client.post(reverse("cnc_resizer:engine_resize"),
                                data=json.dumps({"content": build_kerf_file(),
                                                 "filename": "kerf.cnc",
                                                 "params": {"mode": "equal", "delta": 100,
                                                            "symmetric": True}}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["resizable"])
        self.assertAlmostEqual(data["length_after"] - data["length_before"], 100.0, places=1)
        self.assertTrue(data["cnc_text"].strip())

    def test_engine_error_comes_back_with_status(self):
        resp = self.client.post(reverse("cnc_resizer:engine_resize"),
                                data=json.dumps({"content": build_kerf_file(),
                                                 "filename": "kerf.cnc",
                                                 "params": {"mode": "nope", "delta": 1}}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])

    def test_requires_operator(self):
        User.objects.create_user("plain-resize", password="x")
        self.client.login(username="plain-resize", password="x")
        resp = self.client.post(reverse("cnc_resizer:engine_resize"),
                                data=json.dumps({"content": "N10M30\n", "params": {}}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 403)


# A 100x200 panel with two square hole rows at y=60..70 and y=130..140:
# the whole height is one contour-clean band. Child features must remain
# anchored to the lower edge even when the default stretch band crosses them.
PANEL_TEXT = "\n".join([
    "%",
    "N10G90G54",
    "N20G53 Z0",
    "N30T3M6",
    "N40 M141",
    "N50 M6T3",
    "N60 G43 H3",
    "N70G0X0.000Y0.000Z5.000M3 S16000",
    "N80G1Z-16.000F10000.0",
    "N90G1Y200.000",
    "N100G1X100.000",
    "N110G1Y0.000",
    "N120G1X0.000",
    "N130G0X20.000Y60.000Z5.000M3 S16000",
    "N140G1Z-16.000F10000.0",
    "N150G1X30.000",
    "N160G1Y70.000",
    "N170G1X20.000",
    "N180G1Y60.000",
    "N190G0X20.000Y130.000Z5.000M3 S16000",
    "N200G1Z-16.000F10000.0",
    "N210G1X30.000",
    "N220G1Y140.000",
    "N230G1X20.000",
    "N240G1Y130.000",
    "N250G0Z5.000M3 S16000",
    "N260G53Z0",
    "N270G53X0Y3950",
    "N280M30",
    "%",
]) + "\n"


class StretchLowerAnchorTests(SimpleTestCase):
    """Default stretch keeps child features at their lower-edge offset; the
    stretch band no longer has to be relocated into a child-free gap."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from .engine.parser import parse_program
        from .engine.parts import detect_parts
        cls.detection = detect_parts(parse_program(PANEL_TEXT, filename="panel.cnc"), {})
        cls.part = cls.detection["parts"][0]

    def _edit(self, text, part_id=None, **extra):
        from .engine.part_edits import edits_part_program
        return edits_part_program(text, {"edits": [
            {"op": "stretch_part", "part_id": part_id or self.part["id"],
             "axis": "y", "delta": 40.0, **extra},
        ]}, filename="panel.cnc")

    def test_capability_band_can_cross_hole_groups_without_relocation(self):
        self.assertTrue(self.part["stretch_bands_y"])
        result = self._edit(PANEL_TEXT)
        self.assertTrue(result["validation"]["passed"],
                        msg=str(result["validation"]["errors"]))
        entry = result["edits_applied"][0]
        self.assertNotIn("band_relocated", entry)
        self.assertEqual(entry["band"], self.part["stretch_band_y"])

    def test_child_features_keep_their_distance_from_lower_edge(self):
        from .engine.parser import parse_program
        from .engine.parts import detect_parts
        from .engine.part_edits import edits_part_program
        result = edits_part_program(PANEL_TEXT, {"edits": [
            {"op": "stretch_part", "part_id": self.part["id"], "axis": "y", "delta": 40.0},
        ]}, filename="panel.cnc")
        key = "|".join(self.part["contour_block_ids"])
        after = next(p for p in result["resized"]["parts"]
                     if "|".join(p["contour_block_ids"]) == key)
        self.assertAlmostEqual(after["bbox"]["y_max"] - after["bbox"]["y_min"], 240.0, places=1)
        ys = sorted((c["bbox"]["y_min"], c["bbox"]["y_max"]) for c in after["children"])
        self.assertAlmostEqual(ys[0][0], 60.0, places=1)    # bottom row: pinned
        self.assertAlmostEqual(ys[0][1], 70.0, places=1)
        self.assertAlmostEqual(ys[1][0], 130.0, places=1)   # top row stays lower-anchored
        self.assertAlmostEqual(ys[1][1], 140.0, places=1)

    def test_no_clear_gap_is_no_longer_required_for_parametric_anchor(self):
        # A child spanning nearly the whole height does not need a free cut
        # gap because child geometry remains fixed relative to the lower edge.
        text = PANEL_TEXT.replace(
            "N250G0Z5.000M3 S16000",
            "N245G0X40.000Y0.300Z5.000M3 S16000\n"
            "N246G1Z-6.000F10000.0\n"
            "N247G1X60.000\n"
            "N248G1Y199.700\n"
            "N249G1X40.000\n"
            "N251G1Y0.300\n"
            "N250G0Z5.000M3 S16000")
        result = self._edit(text)
        self.assertTrue(result["validation"]["passed"],
                        msg=str(result["validation"]["errors"]))
        after = next(p for p in result["resized"]["parts"]
                     if p["contour_block_ids"] == self.part["contour_block_ids"])
        self.assertAlmostEqual(after["bbox"]["y_max"] - after["bbox"]["y_min"], 240.0, places=1)

    def test_face_raster_does_not_force_band_relocation(self):
        # A face-raster child may cross the band; lower anchoring keeps it in place.
        text = PANEL_TEXT.replace(
            "N250G0Z5.000M3 S16000",
            "N245G0X10.000Y50.000Z5.000M3 S16000\n"
            "N246G1Z-6.000F10000.0\n"
            "N247G1X90.000\n"
            "N248G1Y70.000\n"
            "N249G1X10.000\n"
            "N251G1Y50.000\n"
            "N252G1X90.000\n"
            "N253G1Y90.000\n"
            "N254G1X10.000\n"
            "N255G1Y110.000\n"
            "N256G1X90.000\n"
            "N257G1Y130.000\n"
            "N258G1X10.000\n"
            "N259G1Y150.000\n"
            "N260G1X90.000\n"
            "N261G1Y170.000\n"
            "N262G1X10.000\n"
            "N250G0Z5.000M3 S16000")
        result = self._edit(text)
        self.assertTrue(result["validation"]["passed"],
                        msg=str(result["validation"]["errors"]))
        entry = result["edits_applied"][0]
        self.assertNotIn("band_relocated", entry)

    def test_explicit_clear_band_needs_no_relocation(self):
        result = self._edit(PANEL_TEXT, band=[70.25, 129.75])
        self.assertTrue(result["validation"]["passed"])
        self.assertNotIn("band_relocated", result["edits_applied"][0])


class LegacyGapDistributionTests(SimpleTestCase):
    """The old per-gap control surface: enabled[] pins gaps, manual[] sets
    exact per-gap increments — both flow through core.process unchanged."""

    def test_disabled_gap_stays_fixed(self):
        from .core import process
        probe = process(build_kerf_file(), {"mode": "equal", "delta": 0.0})
        n = len(probe["gaps"])
        enabled = [True] * n
        enabled[0] = False
        result = process(build_kerf_file(), {"mode": "equal", "delta": 60.0,
                                             "symmetric": False, "enabled": enabled})
        gaps = result["gaps"]
        self.assertFalse(gaps[0]["enabled"])
        self.assertAlmostEqual(gaps[0]["increment"], 0.0, places=6)
        self.assertAlmostEqual(sum(g["increment"] for g in gaps), 60.0, places=1)

    def test_manual_mode_uses_per_gap_values(self):
        from .core import process
        n = None
        probe = process(build_kerf_file(), {"mode": "equal", "delta": 0.0})
        n = len(probe["gaps"])
        manual = [0.0] * n
        manual[0] = 12.5
        result = process(build_kerf_file(), {"mode": "manual", "delta": 0.0,
                                             "symmetric": False,
                                             "enabled": [True] * n,
                                             "manual": manual})
        self.assertAlmostEqual(result["gaps"][0]["increment"], 12.5, places=6)
        for gap in result["gaps"][1:]:
            self.assertAlmostEqual(gap["increment"], 0.0, places=6)
        self.assertAlmostEqual(result["length_after"] - result["length_before"], 12.5, places=1)


def _door_block(x0, y0, x1, y1):
    """One closed rectangular door contour (contour-only part, no children)."""
    return "\n".join([
        "G21 G90 G17",
        f"G0 X{x0:.3f} Y{y0:.3f} Z5.000",
        "G1 Z-16.000 F10000.0",
        f"G1 X{x1:.3f} Y{y0:.3f}",
        f"G1 X{x1:.3f} Y{y1:.3f}",
        f"G1 X{x0:.3f} Y{y1:.3f}",
        f"G1 X{x0:.3f} Y{y0:.3f}",
        "G0 Z5.000",
    ]) + "\n"


class LateralChainStretchTests(SimpleTestCase):
    """Doors / plain panels cut SIDE BY SIDE from one G-code: several copies
    of the same part share the sheet Y-range, so the old axis-overlap guards
    rejected both the chain and the single stretch. 2D-disjoint (lateral)
    parts must stretch independently; only truly interlocked boxes stay
    fail-closed."""

    @classmethod
    def setUpClass(cls):
        from .engine.parser import parse_program
        from .engine.parts import detect_parts
        cls.text = (_door_block(0, 0, 396, 1970)
                    + _door_block(420, 0, 816, 1970)
                    + _door_block(840, 0, 1236, 1970))
        cls.program = parse_program(cls.text)
        detection = detect_parts(cls.program)
        cls.parts = sorted(detection["parts"], key=lambda item: item["bbox"]["x_min"])
        cls.ids = [part["id"] for part in cls.parts]

    def _edit(self, edits):
        from .engine.part_edits import edits_part_program
        return edits_part_program(self.text, {"edits": edits})

    def test_chain_stretches_three_side_by_side_doors_independently(self):
        result = self._edit([{
            "op": "stretch_chain", "axis": "y", "parts": self.ids, "delta": 50.0,
        }])
        self.assertTrue(result["validation"]["passed"],
                        msg=str(result["validation"]["errors"]))
        resized = sorted(result["resized"]["parts"],
                         key=lambda item: item["bbox"]["x_min"])
        for before, after in zip(self.parts, resized):
            self.assertAlmostEqual(after["bbox"]["y_min"], before["bbox"]["y_min"])
            self.assertAlmostEqual(after["bbox"]["y_max"],
                                   before["bbox"]["y_max"] + 50.0, places=1)
            self.assertAlmostEqual(after["bbox"]["x_min"], before["bbox"]["x_min"])
            self.assertAlmostEqual(after["bbox"]["x_max"], before["bbox"]["x_max"])
        offsets = [(entry["lower_offset"], entry["upper_offset"])
                   for entry in result["edits_applied"]
                   if entry.get("op") == "stretch_chain"]
        self.assertEqual(offsets, [(0.0, 50.0), (0.0, 50.0), (0.0, 50.0)])

    def test_single_stretch_ignores_laterally_separated_neighbours(self):
        result = self._edit([{
            "op": "stretch_part", "part_id": self.ids[1], "axis": "y", "delta": 50.0,
        }])
        self.assertTrue(result["validation"]["passed"],
                        msg=str(result["validation"]["errors"]))
        resized = sorted(result["resized"]["parts"],
                         key=lambda item: item["bbox"]["x_min"])
        for index, (before, after) in enumerate(zip(self.parts, resized)):
            if index == 1:
                self.assertAlmostEqual(after["bbox"]["y_max"],
                                       before["bbox"]["y_max"] + 50.0, places=1)
            else:
                self.assertAlmostEqual(after["bbox"]["y_max"],
                                       before["bbox"]["y_max"], places=1)
                self.assertAlmostEqual(after["bbox"]["y_min"],
                                       before["bbox"]["y_min"], places=1)

    def test_chain_follows_stacked_column_but_not_lateral_column(self):
        text = (_door_block(0, 0, 396, 780) + _door_block(420, 0, 816, 780)
                + _door_block(0, 800, 396, 1580))
        from .engine.parser import parse_program
        from .engine.parts import detect_parts
        parts = detect_parts(parse_program(text))["parts"]
        bottom = next(p for p in parts if p["bbox"]["x_min"] < 1 and p["bbox"]["y_min"] < 1)
        lateral = next(p for p in parts if p["bbox"]["x_min"] > 400)
        stacked = next(p for p in parts if p["bbox"]["y_min"] > 790)
        from .engine.part_edits import edits_part_program
        result = edits_part_program(text, {"edits": [{
            "op": "stretch_chain", "axis": "y",
            "parts": [bottom["id"], lateral["id"], stacked["id"]], "delta": 50.0,
        }]})
        self.assertTrue(result["validation"]["passed"],
                        msg=str(result["validation"]["errors"]))
        summary = {entry["part_id"]: entry for entry in result["edits_applied"]
                   if entry.get("op") == "stretch_chain"}
        self.assertAlmostEqual(summary[bottom["id"]]["lower_offset"], 0.0)
        self.assertAlmostEqual(summary[lateral["id"]]["lower_offset"], 0.0)
        self.assertAlmostEqual(summary[stacked["id"]]["lower_offset"], 50.0)

    def test_chain_rejects_truly_interlocked_members(self):
        # L-shaped part + a rectangle nested in its notch: the boxes share
        # area (so the chain is undefined) but the paths never cross, so
        # detection does NOT lock them — the 2D-overlap guard must reject.
        text = "\n".join([
            "G21 G90 G17",
            "G0 X0.000 Y0.000 Z5.000",
            "G1 Z-16.000 F10000.0",
            "G1 X100.000 Y0.000", "G1 X100.000 Y40.000", "G1 X40.000 Y40.000",
            "G1 X40.000 Y100.000", "G1 X0.000 Y100.000", "G1 X0.000 Y0.000",
            "G0 Z5.000",
            "G0 X50.000 Y50.000 Z5.000",
            "G1 Z-16.000 F10000.0",
            "G1 X90.000 Y50.000", "G1 X90.000 Y90.000", "G1 X50.000 Y90.000",
            "G1 X50.000 Y50.000",
            "G0 Z5.000",
        ]) + "\n"
        from .engine.parser import parse_program
        from .engine.parts import detect_parts
        from .engine.part_edits import TransformError, edits_part_program
        parts = detect_parts(parse_program(text))["parts"]
        self.assertEqual(len(parts), 2)
        self.assertFalse(any(part["locked"] for part in parts))
        with self.assertRaises(TransformError) as caught:
            edits_part_program(text, {"edits": [{
                "op": "stretch_chain", "axis": "y",
                "parts": [part["id"] for part in parts], "delta": 50.0,
            }]})
        self.assertIn("هم‌پوشانی دوبعدی", str(caught.exception))
