"""Package D — tool table (میز ابزار): per-parking diameter compensation.

Covers: ToolSlot/ToolSet API (seed, save, sets save/load/delete), per-tool hole
compensation in extract_vectors, the TOOLPATH DXF layer, visible warnings for
parkings missing from the table, end-to-end process wiring, and the DB-side
tool_map injection in the process view.
"""
import json

from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import core
from .models import ToolSet, ToolSlot
from .test_kerf import build_kerf_file


def _text(lines):
    return "\r\n".join(lines) + "\r\n"


def _manager():
    user = User.objects.create_user("cnc-manager-tt", password="testpass")
    group, _created = Group.objects.get_or_create(name="2")
    user.groups.add(group)
    return user


class ToolTableApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = _manager()

    def setUp(self):
        self.client.login(username="cnc-manager-tt", password="testpass")

    def test_get_seeds_ten_parkings(self):
        resp = self.client.get(reverse("cnc_resizer:tools"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual([s["slot"] for s in data["slots"]], list(range(1, 11)))
        self.assertEqual(data["sets"], [])
        self.assertEqual(ToolSlot.objects.count(), 10)

    def test_requires_operator(self):
        User.objects.create_user("plain-tt", password="x")
        self.client.login(username="plain-tt", password="x")
        resp = self.client.get(reverse("cnc_resizer:tools"))
        self.assertEqual(resp.status_code, 403)

    def test_save_and_reload_rows(self):
        rows = [{"slot": 3, "label": "فرز ۶", "dia": 6},
                {"slot": 2, "label": "فرز ۸", "dia": "8.5"},
                {"slot": 5, "label": "", "dia": None}]
        resp = self.client.post(reverse("cnc_resizer:tools"), data=json.dumps({"slots": rows}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ToolSlot.objects.get(slot=3).dia_value, 6.0)
        self.assertEqual(ToolSlot.objects.get(slot=2).dia_value, 8.5)
        self.assertIsNone(ToolSlot.objects.get(slot=5).dia)
        # out-of-range parking and out-of-range diameter are rejected
        resp = self.client.post(reverse("cnc_resizer:tools"),
                                data=json.dumps({"slots": [{"slot": 11, "dia": 6}]}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(reverse("cnc_resizer:tools"),
                                data=json.dumps({"slots": [{"slot": 3, "dia": 0}]}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_set_save_load_delete_roundtrip(self):
        resp = self.client.post(reverse("cnc_resizer:tool_sets"),
                                data=json.dumps({"action": "save", "name": "ست قطعه A"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)  # empty magazine cannot be saved as a set
        self.client.post(reverse("cnc_resizer:tools"),
                         data=json.dumps({"slots": [{"slot": 3, "label": "فرز", "dia": 6},
                                                    {"slot": 10, "label": "بالسر", "dia": 35}]}),
                         content_type="application/json")
        resp = self.client.post(reverse("cnc_resizer:tool_sets"),
                                data=json.dumps({"action": "save", "name": "ست قطعه A"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        # mutate the table, then load the set back
        self.client.post(reverse("cnc_resizer:tools"),
                         data=json.dumps({"slots": [{"slot": 3, "label": "عوض شد", "dia": 25}]}),
                         content_type="application/json")
        resp = self.client.post(reverse("cnc_resizer:tool_sets"),
                                data=json.dumps({"action": "load", "name": "ست قطعه A"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ToolSlot.objects.get(slot=3).dia_value, 6.0)
        self.assertEqual(ToolSlot.objects.get(slot=3).label, "فرز")
        self.assertEqual(ToolSlot.objects.get(slot=10).dia_value, 35.0)
        # unknown set
        resp = self.client.post(reverse("cnc_resizer:tool_sets"),
                                data=json.dumps({"action": "load", "name": "ناموجود"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 404)
        # delete
        resp = self.client.post(reverse("cnc_resizer:tool_sets"),
                                data=json.dumps({"action": "delete", "name": "ست قطعه A"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ToolSet.objects.filter(name="ست قطعه A").exists())

    def test_index_renders_tool_table_card(self):
        resp = self.client.get(reverse("cnc_resizer:index"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "میز ابزار")
        self.assertContains(resp, reverse("cnc_resizer:tools"))
        self.assertContains(resp, reverse("cnc_resizer:tool_sets"))


class ToolCompensationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = build_kerf_file()  # fixture already returns program text
        cls.lines = cls.text.splitlines()

    def _radii(self, dxf, layer):
        ls = dxf.splitlines()
        radii = []
        for i, ln in enumerate(ls):
            if ln.strip() == "CIRCLE" and i + 10 < len(ls) and ls[i + 2].strip() == layer:
                self.assertEqual(ls[i + 9].strip(), "40")
                radii.append(float(ls[i + 10].strip()))
        return sorted(radii)

    def test_tool_map_compensates_by_parking(self):
        vec = core.extract_vectors(self.lines, 8.0, {3: 6.0, 2: 8.0})
        self.assertEqual(len(vec["holes"]), 4)
        for h in vec["holes"]:
            self.assertEqual(h["tool"], 3)
            self.assertAlmostEqual(h["path_dia"], 4.0, places=2)
            self.assertEqual(h["dia"], 10.0)  # 4mm path + T3=6mm from the table
            self.assertEqual(h["tool_dia_used"], 6.0)

    def test_fallback_without_map_keeps_legacy_behavior(self):
        vec = core.extract_vectors(self.lines, 8.0)
        for h in vec["holes"]:
            self.assertEqual(h["dia"], 12.0)  # 4mm path + fallback 8mm
            self.assertEqual(h["tool"], 3)

    def test_dxf_has_toolpath_layer_with_raw_path(self):
        dxf = core.to_dxf(core.extract_vectors(self.lines, 8.0, {3: 6.0}))
        self.assertIn("TOOLPATH", dxf)
        self.assertEqual(self._radii(dxf, "TOOLPATH"), [2.0] * 4)   # raw centerline
        self.assertEqual(self._radii(dxf, "HOLES"), [5.0] * 4)      # compensated 10mm

    def test_process_end_to_end_with_tool_map(self):
        res = core.process(self.text, {"delta": 10, "tool_dia": 8.0, "tool_map": {"3": 6.0, "2": 8.0}})
        self.assertTrue(res["ok"])
        joined = "\n".join(res["warnings"])
        self.assertIn("T3 = 6mm", joined)
        self.assertIn("T2 = 8mm", joined)
        for h in res["resized"]["holes"]:
            self.assertEqual(h["dia"], 10.0)

    def test_kerf_mode_with_tool_map(self):
        res = core.process(self.text, {"mode": "kerf", "delta": 28, "tool_dia": 8.0, "tool_map": {3: 6.0}})
        self.assertTrue(res["ok"])
        joined = "\n".join(res["warnings"])
        self.assertIn("T3 = 6mm", joined)
        self.assertIn("T2", joined)  # T2 has no entry → visible fallback warning
        self.assertIn("پیش‌فرض 8mm", joined)
        for h in res["resized"]["holes"]:
            self.assertEqual(h["dia"], 10.0)

    def test_missing_parking_warning_lists_default(self):
        res = core.process(self.text, {"delta": 0, "tool_dia": 8.0, "tool_map": {3: 6.0}})
        joined = "\n".join(res["warnings"])
        self.assertIn("T2", joined)
        self.assertIn("پیش‌فرض 8mm", joined)

    def test_junk_tool_map_entries_are_dropped(self):
        res = core.process(self.text, {"delta": 0, "tool_dia": 8.0,
                                       "tool_map": {"3": "abc", "55": 6.0, 3: None, "2": "8"}})
        self.assertTrue(res["ok"])
        # {"2": 8} survived → T2 compensated with 8mm, T3 fell back to 8mm too
        joined = "\n".join(res["warnings"])
        self.assertIn("T2 = 8mm", joined)
        self.assertIn("T3", joined)


class ProcessToolMapInjectionTests(TestCase):
    """The process view must override any client-sent tool_map with the DB table."""

    @classmethod
    def setUpTestData(cls):
        cls.manager = _manager()

    def test_db_tool_table_wins(self):
        ToolSlot.objects.create(slot=3, label="فرز", dia=6)
        self.client.login(username="cnc-manager-tt", password="testpass")
        payload = {"content": build_kerf_file(),
                   "params": {"delta": 0, "tool_dia": 8.0, "tool_map": {"9": 99}}}
        resp = self.client.post(reverse("cnc_resizer:process"), data=json.dumps(payload),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        for h in data["resized"]["holes"]:
            self.assertEqual(h["dia"], 10.0)  # DB table (T3=6) beat the client's junk
        self.assertIn("T3 = 6mm", "\n".join(data["warnings"]))
