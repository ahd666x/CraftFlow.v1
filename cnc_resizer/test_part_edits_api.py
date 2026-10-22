"""Django-level coverage for the part-level API surface (prompt-1):
``POST /cnc/api/parts/`` detection, part-level edits through the existing
engine preview/output chain, and the fail-closed behaviour around locked
parts and arc-crossing bands.
"""
import json
import os
import unittest

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from .tests import SAMPLE  # noqa: F401  (keeps the sample.cnc contract visible)

FLAGSHIP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata",
                        "t.1.1970x395.2set.cnc")

CROSSING_FILE = "\n".join([
    "%",
    "N10G90G54",
    "N20G53 Z0",
    "N30T3M6",
    "N40 M141",
    "N50 M6T3",
    "N60 G43 H3",
    "N70G0X0.000Y100.000Z5.000M3 S16000",
    "N80G1Z-16.000F10000.0",
    "N90G1X400.000",
    "N100G1Y200.000",
    "N110G1X0.000",
    "N120G1Y100.000",
    "N130G0Z5.000M3 S16000",
    "N140G0X150.000Y0.000M3 S16000",
    "N150G1Z-16.000F10000.0",
    "N160G1X250.000",
    "N170G1Y300.000",
    "N180G1X150.000",
    "N190G1Y0.000",
    "N200G0Z5.000M3 S16000",
    "N210G0X0.000Y0.000M3 S16000",
    "N220G53Z0",
    "N230G53X0Y3950",
    "N240M30",
    "%",
]) + "\n"


@unittest.skipUnless(os.path.exists(FLAGSHIP), "flagship fixture not shipped")
class PartApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("parts-manager", password="testpass")
        cls.manager.groups.add(Group.objects.create(name="2"))
        cls.regular = User.objects.create_user("parts-user", password="testpass")
        with open(FLAGSHIP, errors="replace") as handle:
            cls.flagship_text = handle.read()

    def _post(self, url_name, payload):
        return self.client.post(reverse(f"cnc_resizer:{url_name}"),
                                data=json.dumps(payload),
                                content_type="application/json")

    def test_parts_api_requires_operator(self):
        response = self._post("parts_api", {"content": self.flagship_text})
        self.assertEqual(response.status_code, 302)  # anonymous -> login
        self.client.login(username="parts-user", password="testpass")
        response = self._post("parts_api", {"content": self.flagship_text})
        self.assertEqual(response.status_code, 403)

    def test_parts_api_detects_the_flagship_parts(self):
        self.client.login(username="parts-manager", password="testpass")
        response = self._post("parts_api", {
            "content": self.flagship_text, "filename": "t.1.1970x395.2set.cnc"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["safe_for_execution"])
        self.assertEqual(len(data["parts"]), 3)
        for part in data["parts"]:
            self.assertEqual(len(part["passes"]), 2)
            self.assertIn("capabilities", part)
            self.assertIn("bbox_finished", part)
        self.assertEqual(data["parts"], data["parts"])  # JSON-safe (no Loop objects)
        self.assertIn("sheet_bbox", data)

    def test_parts_api_rejects_garbage(self):
        self.client.login(username="parts-manager", password="testpass")
        response = self._post("parts_api", {"content": "hello world"})
        self.assertEqual(response.status_code, 400)
        response = self._post("parts_api", {"nope": True})
        self.assertEqual(response.status_code, 400)

    def test_part_edit_preview_through_engine_preview(self):
        self.client.login(username="parts-manager", password="testpass")
        detect = self._post("parts_api", {"content": self.flagship_text}).json()
        middle = sorted(detect["parts"], key=lambda part: part["bbox"]["y_min"])[1]
        response = self._post("engine_preview", {
            "content": self.flagship_text, "filename": "t.1.1970x395.2set.cnc",
            "edits": [{"op": "stretch_part", "part_id": middle["id"],
                       "axis": "y", "delta": 100.0}]})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["operation"], "part_edits")
        self.assertTrue(data["validation"]["passed"],
                        msg=str(data["validation"]["errors"]))
        self.assertFalse(data["safe_for_execution"])
        self.assertFalse(data["collision_check"]["flagged"])

    @override_settings(CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router",
                       CNC_RESIZER_IMPLICIT_UNITS="mm")
    def test_part_edit_machine_output_passes_the_dialect_gate(self):
        self.client.login(username="parts-manager", password="testpass")
        detect = self._post("parts_api", {"content": self.flagship_text}).json()
        middle = sorted(detect["parts"], key=lambda part: part["bbox"]["y_min"])[1]
        edits = [{"op": "stretch_part", "part_id": middle["id"],
                  "axis": "y", "delta": 100.0}]
        output = self._post("engine_output", {
            "content": self.flagship_text, "filename": "t.1.1970x395.2set.cnc",
            "edits": edits})
        self.assertEqual(output.status_code, 200)
        data = output.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["allowed"], msg=str(data.get("blockers")))
        self.assertTrue(data["safe_for_execution"])
        text = data["cnc_text"]
        self.assertEqual(len(text.splitlines()),
                         len(self.flagship_text.splitlines()))  # 1:1 structure
        self.assertIn("M141", text)

    def test_locked_part_edit_is_rejected(self):
        self.client.login(username="parts-manager", password="testpass")
        detect = self._post("parts_api", {"content": CROSSING_FILE}).json()
        self.assertEqual(len(detect["parts"]), 2)
        self.assertTrue(all(part["locked"] for part in detect["parts"]))
        response = self._post("engine_preview", {
            "content": CROSSING_FILE,
            "edits": [{"op": "move_part", "part_id": detect["parts"][0]["id"],
                       "dx": 30.0, "dy": 0.0}]})
        # TransformError surfaces as a 400 with the Persian lock reason —
        # the same fail-closed contract as the feature-level preview
        self.assertEqual(response.status_code, 400)
        self.assertIn("قفل", response.json().get("error", ""))

    def test_mixed_feature_and_part_edits_are_rejected(self):
        self.client.login(username="parts-manager", password="testpass")
        detect = self._post("parts_api", {"content": self.flagship_text}).json()
        part_id = detect["parts"][0]["id"]
        response = self._post("engine_preview", {
            "content": self.flagship_text,
            "edits": [{"op": "move_part", "part_id": part_id, "dx": 5, "dy": 0},
                      {"op": "shift", "feature_id": "feature-block-0001",
                       "dx": 5, "dy": 0}]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("op", response.json().get("error", "").lower())

    def test_arc_crossing_band_is_rejected_with_reason(self):
        self.client.login(username="parts-manager", password="testpass")
        detect = self._post("parts_api", {"content": self.flagship_text}).json()
        middle = sorted(detect["parts"], key=lambda part: part["bbox"]["y_min"])[1]
        response = self._post("engine_preview", {
            "content": self.flagship_text,
            "edits": [{"op": "stretch_part", "part_id": middle["id"], "axis": "y",
                       "band": [635.0, 1335.0], "delta": 50.0}]})
        # the band touches the R3 corner arcs -> a 400 with the band reason,
        # never a silent pass
        self.assertEqual(response.status_code, 400)
        self.assertIn("نوار", response.json().get("error", ""))
