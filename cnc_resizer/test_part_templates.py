"""Tests for the parametric part-template package (بستهٔ ق).

Covers: role capture, geometry matching (identity, translation, scaling,
rejection paths), edit materialization, the three API endpoints and the
workbench page wiring. The fail-closed preview chain itself is covered by
test_edits/test_serializer; here we prove the *matching* layer and that
applied edits go through the same chain."""
import json

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from product.models import Material, Part

from .models import CncPartProfile, CncFile
from .part_templates import (capture_roles, envelope_of_features, materialize_edits,
                             match_roles, usable_features)

BASE_FILE = "\n".join([
    "G21 G90 G17",
    "G0 X10 Y10", "G1 X30 Y10", "G1 X30 Y30", "G1 X10 Y30", "G1 X10 Y10",
    "G0 X60 Y10", "G1 X80 Y10", "G1 X80 Y30", "G1 X60 Y30", "G1 X60 Y10",
    "G0 Z5", "M30", "",
])

SHIFT_EDITS = [{"op": "shift", "feature_id": "feature-block-0001", "dx": 5.0, "dy": 0.0}]


def translated_file(dx, dy=0.0):
    """BASE_FILE with every X/Y coordinate shifted (same part, new origin)."""
    def shift_number(value, delta, axis):
        return value + delta
    out = []
    for line in BASE_FILE.splitlines():
        words = line.split()
        new_words = []
        for word in words:
            if word.startswith("X"):
                new_words.append(f"X{float(word[1:]) + dx:.3f}")
            elif word.startswith("Y"):
                new_words.append(f"Y{float(word[1:]) + dy:.3f}")
            else:
                new_words.append(word)
        out.append(" ".join(new_words))
    return "\n".join(out)


def features_of(text):
    from .engine.parser import parse_program
    from .engine.recognition import recognize
    return recognize(parse_program(text))["features"]


def fake_feature(fid, kind, x_min, y_min, width, height):
    return {"id": fid, "kind": kind,
            "bounds": {"x_min": x_min, "y_min": y_min, "x_max": x_min + width, "y_max": y_min + height},
            "center": {"x": x_min + width / 2, "y": y_min + height / 2}}


class PartTemplateMatchingTests(TestCase):
    """Pure-function tests of the role matcher."""

    def setUp(self):
        self.features = features_of(BASE_FILE)
        self.roles = capture_roles(self.features, SHIFT_EDITS)
        self.bounds = envelope_of_features(usable_features(self.features))

    def test_capture_builds_one_role_per_referenced_feature(self):
        self.assertEqual([role["ref"] for role in self.roles], ["feature-block-0001"])
        self.assertEqual(self.roles[0]["kind"], "unclassified_path")

    def test_capture_rejects_unknown_feature(self):
        with self.assertRaises(ValueError) as ctx:
            capture_roles(self.features, [{"op": "shift", "feature_id": "feature-block-9999", "dx": 1, "dy": 1}])
        self.assertIn("نمی‌تواند نقش", str(ctx.exception))

    def test_identical_file_maps_same_ids(self):
        mapping, report = match_roles(self.roles, self.bounds, self.features,
                                      source_feature_count=len(usable_features(self.features)))
        self.assertEqual(mapping, {"feature-block-0001": "feature-block-0001"})
        self.assertEqual(report[0]["dx"], 0.0)

    def test_translated_file_matches(self):
        shifted = features_of(translated_file(50, 25))
        mapping, _ = match_roles(self.roles, self.bounds, shifted,
                                 source_feature_count=len(usable_features(shifted)))
        self.assertEqual(mapping, {"feature-block-0001": "feature-block-0001"})

    def test_scaled_variation_matches(self):
        """Same part doubled in size: scaled expected centers/size still match."""
        doubled = [fake_feature("feature-block-0001", "unclassified_path", 20, 20, 40, 40),
                   fake_feature("feature-block-0002", "unclassified_path", 120, 20, 40, 40)]
        mapping, _ = match_roles(self.roles, self.bounds, doubled, source_feature_count=2)
        self.assertEqual(mapping, {"feature-block-0001": "feature-block-0001"})

    def test_feature_count_mismatch_rejected(self):
        single = features_of("\n".join([
            "G21 G90 G17",
            "G0 X10 Y10", "G1 X30 Y10", "G1 X30 Y30", "G1 X10 Y30", "G1 X10 Y10",
            "G0 Z5", "M30", "",
        ]))
        with self.assertRaises(ValueError) as ctx:
            match_roles(self.roles, self.bounds, single, source_feature_count=2)
        self.assertIn("تعداد ویژگی", str(ctx.exception))

    def test_moved_beyond_tolerance_rejected(self):
        moved = features_of(BASE_FILE.replace("X60 Y10", "X110 Y10")
                            .replace("X80 Y10", "X130 Y10")
                            .replace("X80 Y30", "X130 Y30")
                            .replace("X60 Y30", "X110 Y30"))
        with self.assertRaises(ValueError) as ctx:
            match_roles(self.roles, self.bounds, moved,
                        source_feature_count=len(usable_features(moved)))
        self.assertIn("مطابق پیدا نکرد", str(ctx.exception))

    def test_kind_mismatch_rejected(self):
        other_kind = [fake_feature("feature-block-0001", "small_loop_candidate", 10, 10, 20, 20),
                      fake_feature("feature-block-0002", "unclassified_path", 60, 10, 20, 20)]
        with self.assertRaises(ValueError):
            match_roles(self.roles, self.bounds, other_kind, source_feature_count=2)

    def test_ambiguous_tie_rejected(self):
        """Two identical candidates straddling the expected center: reject.

        Geometry must be scale-consistent (the envelope comes from the
        features themselves): two same-size features 3mm apart make the
        rel=0.5 expected center their exact midpoint — a 1.5mm/1.5mm tie."""
        source_bounds = {"x_min": 0, "y_min": 0, "x_max": 23, "y_max": 20}
        roles = [{"ref": "R1", "kind": "hole", "rel_cx": 0.5, "rel_cy": 0.5,
                  "size": {"width": 20, "height": 20}}]
        straddling = [fake_feature("F1", "hole", 49, 40, 20, 20),
                      fake_feature("F2", "hole", 52, 40, 20, 20)]
        with self.assertRaises(ValueError) as ctx:
            match_roles(roles, source_bounds, straddling, source_feature_count=2)
        self.assertIn("مبهم", str(ctx.exception))

    def test_identical_twins_with_identical_edits_assign_as_batch(self):
        """Deep-repeat twins (same XY, edited as a broadcast group) are
        interchangeable: identical roles + identical edit semantics assign
        deterministically instead of rejecting as ambiguous."""
        source_bounds = {"x_min": 30, "y_min": 30, "x_max": 50, "y_max": 50}
        roles = [{"ref": "R1", "kind": "hole", "rel_cx": 0.5, "rel_cy": 0.5,
                  "size": {"width": 20, "height": 20}},
                 {"ref": "R2", "kind": "hole", "rel_cx": 0.5, "rel_cy": 0.5,
                  "size": {"width": 20, "height": 20}}]
        edits = [{"op": "shift", "feature_id": "R1", "dx": 5.0, "dy": 0.0},
                 {"op": "shift", "feature_id": "R2", "dx": 5.0, "dy": 0.0}]
        twins = [fake_feature("F1", "hole", 30, 30, 20, 20),
                 fake_feature("F2", "hole", 30, 30, 20, 20)]
        mapping, report = match_roles(roles, source_bounds, twins, source_feature_count=2,
                                      edits=edits)
        self.assertEqual(sorted(mapping.values()), ["F1", "F2"])
        self.assertEqual(mapping["R1"], "F1")
        self.assertEqual(mapping["R2"], "F2")

    def test_identical_twins_with_different_edits_still_rejected(self):
        """Same geometry but DIFFERENT edit semantics per twin: the tie is
        genuinely ambiguous and stays fail-closed."""
        source_bounds = {"x_min": 30, "y_min": 30, "x_max": 50, "y_max": 50}
        roles = [{"ref": "R1", "kind": "hole", "rel_cx": 0.5, "rel_cy": 0.5,
                  "size": {"width": 20, "height": 20}},
                 {"ref": "R2", "kind": "hole", "rel_cx": 0.5, "rel_cy": 0.5,
                  "size": {"width": 20, "height": 20}}]
        edits = [{"op": "shift", "feature_id": "R1", "dx": 5.0, "dy": 0.0},
                 {"op": "shift", "feature_id": "R2", "dx": 9.0, "dy": 0.0}]
        twins = [fake_feature("F1", "hole", 30, 30, 20, 20),
                 fake_feature("F2", "hole", 30, 30, 20, 20)]
        with self.assertRaises(ValueError) as ctx:
            match_roles(roles, source_bounds, twins, source_feature_count=2, edits=edits)
        self.assertIn("مبهم", str(ctx.exception))

    def test_materialize_rewrites_all_id_fields(self):
        mapping = {"feature-block-0001": "feature-A", "feature-block-0002": "feature-B"}
        payload = [{"op": "shift", "feature_id": "feature-block-0001", "dx": 1, "dy": 2},
                   {"op": "center_on", "feature_id": "feature-block-0002", "axis": "x",
                    "anchor_feature_id": "feature-block-0001"},
                   {"op": "center_between", "feature_id": "feature-block-0002", "axis": "x",
                    "anchor_feature_id": "feature-block-0001", "second_feature_id": "feature-block-0001"}]
        concrete = materialize_edits(payload, mapping)
        self.assertEqual(concrete[0]["feature_id"], "feature-A")
        self.assertEqual(concrete[1]["anchor_feature_id"], "feature-A")
        self.assertEqual(concrete[2]["second_feature_id"], "feature-A")
        self.assertEqual(payload[0]["feature_id"], "feature-block-0001")  # source untouched

    def test_materialize_rejects_unmapped_role(self):
        with self.assertRaises(ValueError) as ctx:
            materialize_edits([{"op": "shift", "feature_id": "feature-block-0007", "dx": 1, "dy": 1}],
                              {"feature-block-0001": "feature-A"})
        self.assertIn("نقش", str(ctx.exception))


class PartTemplateApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = User.objects.create_user("cnc-operator", password="testpass")
        group = Group.objects.create(name="2")
        cls.operator.groups.add(group)
        cls.part = Part.objects.create(
            material=Material.objects.create(name="MDF", thickness=16),
            name="درب آشپزخانه", length=1090, width=450, pname="کابینت",
            routing_code="CNC",
        )
        base = CncFile.objects.create(
            barcode_key="T16", path="/tmp/t.16.cnc", sha256="a" * 64,
        )
        CncPartProfile.objects.create(part=cls.part, source_file=base)

    def setUp(self):
        self.client.login(username="cnc-operator", password="testpass")
        self.list_url = reverse("cnc_resizer:part_templates")
        self.apply_url = reverse("cnc_resizer:part_template_apply")

    def _save_template(self, name="درب ۲ست", content=BASE_FILE, edits=None, part_id=None):
        return self.client.post(self.list_url,
                                data=json.dumps({"name": name, "part_id": part_id or self.part.pk,
                                                 "content": content, "filename": "t.16.cnc",
                                                 "edits": edits or SHIFT_EDITS}),
                                content_type="application/json")

    def test_save_captures_roles_and_lists(self):
        response = self._save_template()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["role_count"], 1)
        listing = self.client.get(self.list_url).json()
        self.assertEqual(len(listing["templates"]), 1)
        self.assertEqual(listing["templates"][0]["part_id"], self.part.pk)
        self.assertIn(self.part.name, listing["templates"][0]["part_label"])
        option_labels = [option["label"] for option in listing["part_options"]]
        self.assertTrue(any(self.part.name in label for label in option_labels))

    def test_save_upserts_by_part_and_name(self):
        first = self._save_template()
        second = self._save_template(edits=[{"op": "shift", "feature_id": "feature-block-0001",
                                             "dx": 7.0, "dy": 0.0}])
        self.assertTrue(first.json()["created"])
        self.assertFalse(second.json()["created"])
        from .models import CncPartTemplate
        template = CncPartTemplate.objects.get()
        self.assertEqual(template.payload[0]["dx"], 7.0)

    def test_save_requires_name_and_part(self):
        response = self._save_template(name="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("نام قالب", response.json()["error"])
        response = self._save_template(part_id=999999)
        self.assertEqual(response.status_code, 400)
        self.assertIn("پیدا نشد", response.json()["error"])

    def test_save_rejects_unmatchable_feature(self):
        response = self._save_template(name="بد", edits=[{"op": "shift", "feature_id": "feature-x", "dx": 1, "dy": 1}])
        self.assertEqual(response.status_code, 400)

    def test_apply_returns_concrete_edits_and_preview(self):
        self._save_template()
        response = self.client.post(self.apply_url,
                                    data=json.dumps({"content": BASE_FILE, "filename": "t.16.cnc",
                                                     "template_id": 1}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["edits"][0]["feature_id"], "feature-block-0001")
        self.assertTrue(data["preview"]["validation"]["passed"])
        self.assertEqual(data["mapping"][0]["dx"], 0.0)

    def test_apply_to_translated_file_remaps_and_passes(self):
        self._save_template()
        response = self.client.post(self.apply_url,
                                    data=json.dumps({"content": translated_file(50, 25),
                                                     "filename": "new.cnc", "template_id": 1}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data["preview"]["validation"]["passed"])
        self.assertEqual(data["mapping"][0]["matched"], "feature-block-0001")

    def test_apply_rejects_foreign_file_fail_closed(self):
        self._save_template()
        single = "\n".join([
            "G21 G90 G17",
            "G0 X10 Y10", "G1 X30 Y10", "G1 X30 Y30", "G1 X10 Y30", "G1 X10 Y10",
            "G0 Z5", "M30", "",
        ])
        response = self.client.post(self.apply_url,
                                    data=json.dumps({"content": single, "filename": "other.cnc",
                                                     "template_id": 1}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("تعداد ویژگی", response.json()["error"])

    def test_apply_unknown_template_404ish(self):
        response = self.client.post(self.apply_url,
                                    data=json.dumps({"content": BASE_FILE, "filename": "t.16.cnc",
                                                     "template_id": 4242}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("پیدا نشد", response.json()["error"])

    def test_delete_template(self):
        self._save_template()
        url = reverse("cnc_resizer:part_template_delete", args=[1])
        response = self.client.post(url, content_type="application/json")
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.client.get(self.list_url).json()["templates"], [])

    def test_requires_operator_group(self):
        self.client.logout()
        User.objects.create_user("plain", password="testpass")
        self.client.login(username="plain", password="testpass")
        self.assertEqual(self.client.get(self.list_url).status_code, 403)


class PartTemplateWorkbenchPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = User.objects.create_user("cnc-operator", password="testpass")
        group = Group.objects.create(name="2")
        cls.operator.groups.add(group)

    def test_part_template_apis_survive_the_workbench_merge(self):
        """پرامپت ۲ فاز «د»: میزکار به نقشهٔ اصلی ادغام شد؛ selectهای قالب
        قطعه طبق طرح حذف شده‌اند ولی APIهای قالب قطعه سرِ جای خودشان زنده‌اند
        (تست‌های API همین فایل) و صفحهٔ نقشه‌محور سیم‌کشی الگوی ویرایش را دارد."""
        self.client.login(username="cnc-operator", password="testpass")
        response = self.client.get(reverse("cnc_resizer:engine_workbench"))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("cnc_resizer:index"), fetch_redirect_response=False)
        page = self.client.get(reverse("cnc_resizer:index"))
        self.assertEqual(page.status_code, 200)
        for fragment in ('/cnc/api/edit-templates/', 'btnHistory', 'historyPanel'):
            self.assertContains(page, fragment)
