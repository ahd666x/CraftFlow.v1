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
