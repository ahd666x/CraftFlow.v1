import os
import shutil
import tempfile
import json
import hashlib

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from decimal import Decimal

from product.models import Material, Part, Product, ProductBOM, ProductCategory

from .models import CncControllerProfile, CncEngineeringProfile, CncFile, CncPartProfile
from .services import CncResolutionError, canonical_barcode_key, register_base_file, resolve_cnc_file
from .tests import SAMPLE


class CncIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("cnc-manager", password="testpass")
        manager_group = Group.objects.create(name="2")
        cls.manager.groups.add(manager_group)
        cls.manager2 = User.objects.create_user("cnc-manager-2", password="testpass")
        cls.manager2.groups.add(manager_group)
        cls.regular = User.objects.create_user("cnc-user", password="testpass")

    def test_resizer_requires_manager(self):
        response = self.client.get(reverse("cnc_resizer:index"))
        self.assertEqual(response.status_code, 302)
        self.client.login(username="cnc-user", password="testpass")
        response = self.client.get(reverse("cnc_resizer:index"))
        self.assertEqual(response.status_code, 403)

    def test_manager_can_open_resizer(self):
        self.client.login(username="cnc-manager", password="testpass")
        response = self.client.get(reverse("cnc_resizer:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/cnc/api/process/")
        self.assertContains(response, "/cnc/api/analyze/")
        self.assertContains(response, "/cnc/engine/")

    def test_new_engine_page_and_analysis_api_are_preview_only(self):
        self.client.login(username="cnc-manager", password="testpass")
        page = self.client.get(reverse("cnc_resizer:engine_workbench"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "حالت ایمن و فقط پیش‌نمایش")
        self.assertContains(page, "شروع ثابت، پایان جابه‌جا")
        self.assertContains(page, "min_to_min")
        self.assertContains(page, "center_to_center")
        self.assertContains(page, "$('source').addEventListener('input'")
        self.assertContains(page, "if(other.id===f.id||!other.bounds)continue")
        content = "\n".join(["G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-2", "G1 X10 Y0",
                              "G1 X10 Y100", "G1 X20 Y100", "G1 X20 Y0", ""])
        response = self.client.post(
            reverse("cnc_resizer:engine_analyze"),
            data=json.dumps({"filename": "small.cnc", "content": content}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertFalse(payload["safe_for_execution"])
        self.assertFalse(payload["execution_gate"]["allowed"])
        feature_id = payload["analysis"]["features"][0]["id"]
        preview = self.client.post(
            reverse("cnc_resizer:engine_preview"),
            data=json.dumps({"content": content, "profile": {
                "axis": "y", "target_dimension": 120, "reference_start": 0, "reference_end": 100,
                "features": {feature_id: "stretch"},
                "operator_interpretations": {feature_id: "surface_or_raster_candidate"},
            }}),
            content_type="application/json",
        )
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertTrue(preview.json()["preview_only"])
        self.assertNotIn("cnc_text", preview.json())
        self.assertFalse(preview.json()["safe_for_execution"])
        self.assertFalse(preview.json()["operator_review"]["physical_operation_confirmed"])
        self.assertEqual(preview.json()["operator_review"]["features"][0]["selected_interpretation"], "surface_or_raster_candidate")
        missing_rule = self.client.post(
            reverse("cnc_resizer:engine_preview"),
            data=json.dumps({"content": content, "profile": {
                "axis": "y", "target_dimension": 120, "reference_start": 0, "reference_end": 100,
                "features": {},
            }}),
            content_type="application/json",
        )
        self.assertEqual(missing_rule.status_code, 400)

    def test_engine_workbench_profile_is_persisted_by_exact_hash_and_private_to_owner(self):
        self.client.login(username="cnc-manager", password="testpass")
        content = "\n".join(["G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-2", "G1 X10 Y0", "G1 Y100",
                              "G1 X20 Y100", "G1 Y0", ""])
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        profile = {
            "axis": "y", "target_dimension": 100, "reference_start": 0, "reference_end": 100,
            "features": {"feature-block-0001": "fixed"},
            "operator_interpretations": {"feature-block-0001": "other_candidate"},
        }
        saved = self.client.post(reverse("cnc_resizer:engine_profile"),
                                 data=json.dumps({"filename": "panel.cnc", "content": content, "profile": profile}),
                                 content_type="application/json")
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertTrue(saved.json()["saved"])
        self.assertEqual(CncEngineeringProfile.objects.get(owner=self.manager).source_sha256, digest)
        loaded = self.client.get(reverse("cnc_resizer:engine_profile"), {"sha256": digest})
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.json()["profile"], profile)
        self.client.logout()
        self.client.login(username="cnc-manager-2", password="testpass")
        private = self.client.get(reverse("cnc_resizer:engine_profile"), {"sha256": digest})
        self.assertEqual(private.status_code, 200)
        self.assertFalse(private.json()["found"])

    def test_engine_preflight_reports_blockers_without_generating_output(self):
        self.client.login(username="cnc-manager", password="testpass")
        content = "\n".join(["G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-2", "G1 X10 Y0", "G1 Y100",
                              "G1 X20 Y100", "G1 Y0", ""])
        response = self.client.post(reverse("cnc_resizer:engine_analyze"),
                                    data=json.dumps({"content": content}), content_type="application/json")
        feature_id = response.json()["analysis"]["features"][0]["id"]
        preflight = self.client.post(reverse("cnc_resizer:engine_preflight"), data=json.dumps({
            "content": content,
            "profile": {"axis": "y", "target_dimension": 100, "reference_start": 0, "reference_end": 100,
                        "features": {feature_id: "fixed"}},
            "machine_limits": {"x_min": -100, "x_max": 100, "y_min": 0, "y_max": 200},
        }), content_type="application/json")
        self.assertEqual(preflight.status_code, 200, preflight.content)
        report = preflight.json()["preflight"]
        self.assertFalse(report["allowed"])
        self.assertFalse(report["output_generated"])
        self.assertEqual(report["execution_gate"], "blocked")
        self.assertTrue(any("output serializer" in reason for reason in report["blockers"]))

    def test_legacy_process_endpoint_never_returns_machine_or_dxf_output(self):
        self.client.login(username="cnc-manager", password="testpass")
        with open(SAMPLE, "r", encoding="utf-8") as stream:
            content = stream.read()
        response = self.client.post(
            reverse("cnc_resizer:process"),
            data=json.dumps({"content": content, "params": {"delta": 0}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["preview_only"])
        self.assertFalse(payload["safe_for_execution"])
        self.assertFalse(payload["machine_output_written"])
        self.assertNotIn("cnc_text", payload)
        self.assertNotIn("dxf_text", payload)

    def test_analyze_api_returns_feature_inventory_for_ui(self):
        self.client.login(username="cnc-manager", password="testpass")
        with open(SAMPLE, "r", encoding="utf-8") as stream:
            content = stream.read()
        response = self.client.post(
            reverse("cnc_resizer:analyze"),
            data=json.dumps({"content": content, "params": {}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("features", payload)
        self.assertIn("ready", payload)

    def test_feature_profile_save_is_validated_and_does_not_approve_legacy_profile(self):
        self.client.login(username="cnc-manager", password="testpass")
        material = Material.objects.create(name="Feature MDF", thickness=Decimal("16.0"))
        part = Part.objects.create(
            material=material, name="Feature panel", length=Decimal("400"),
            width=Decimal("300"), f3="feature.400x300", routing_code="cnc",
        )
        with tempfile.NamedTemporaryFile(suffix=".cnc", delete=False) as stream:
            with open(SAMPLE, "rb") as sample:
                stream.write(sample.read())
            source_path = stream.name
        try:
            source = register_base_file(source_path, part.f3, self.manager)
            with open(source_path, "r", encoding="utf-8") as stream:
                content = stream.read()
            from . import core
            inventory = core.analyze(content)["features"]
            feature_profile = {
                "axis": "length",
                "features": {feature["id"]: {"mode": "fixed"} for feature in inventory},
                "reference_points": {"start": 0, "end": 400},
            }
            response = self.client.post(
                reverse("cnc_resizer:part_profile") + f"?part_id={part.pk}",
                data=json.dumps({
                    "part_id": part.pk, "source_id": source.pk,
                    "params": {"mode": "equal", "symmetric": True, "axis": "length"},
                    "feature_profile": feature_profile,
                }),
                content_type="application/json",
            )
        finally:
            os.unlink(source_path)
        self.assertEqual(response.status_code, 200, response.content)
        profile = CncPartProfile.objects.get(part=part)
        self.assertEqual(profile.feature_profile, feature_profile)
        self.assertEqual(profile.feature_source_file_id, source.pk)
        self.assertFalse(profile.approved)

    def test_archive_revision_is_indexed_and_validated(self):
        with tempfile.NamedTemporaryFile(suffix=".cnc", delete=False) as stream:
            with open(SAMPLE, "rb") as sample:
                stream.write(sample.read())
            path = stream.name
        try:
            record = register_base_file(path, "panel.1740.5x500.cnc", self.manager)
        finally:
            os.unlink(path)
        self.assertEqual(record.barcode_key, "panel.1740.5x500")
        self.assertTrue(record.resizable)
        self.assertEqual(CncFile.objects.count(), 1)

    def test_barcode_key_removes_item_suffix_without_losing_decimals(self):
        self.assertEqual(
            canonical_barcode_key("panel.1740.5x500.item12.cnc"),
            "panel.1740.5x500",
        )

    def _dynamic_part_with_base_file(self):
        """Base+dynamic parts plus an indexed, resize-capable base file."""
        category = ProductCategory.objects.create(name="CNC category")
        product = Product.objects.create(category=category, name="CNC product", base_price=1)
        material = Material.objects.create(name="MDF", thickness=Decimal("16.0"))
        base = Part.objects.create(
            material=material, name="Panel", length=Decimal("100.0"), width=Decimal("50.0"),
            f3="panel.100x50", pname="CNC product", routing_code="cnc",
        )
        ProductBOM.objects.create(product=product, part=base)
        dynamic = Part.objects.create(
            material=material, base_part=base, name=base.name,
            length=Decimal("120.0"), width=Decimal("50.0"),
            f3="panel.120x50.item1", pname=base.pname, routing_code="cnc",
        )
        archive = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, archive, ignore_errors=True)
        source_path = os.path.join(archive, "panel.100x50.cnc")
        with open(SAMPLE, "rb") as sample, open(source_path, "wb") as stream:
            stream.write(sample.read())
        source = register_base_file(source_path, base.f3, self.manager)
        return base, dynamic, source

    def test_resolver_generates_derived_file_from_approved_profile(self):
        base, dynamic, source = self._dynamic_part_with_base_file()
        derived_dir = os.path.join(tempfile.gettempdir(), f"cnc_derived_{source.pk}")
        self.addCleanup(shutil.rmtree, derived_dir, ignore_errors=True)
        with self.settings(CNC_DERIVED_DIR=derived_dir):
            CncPartProfile.objects.create(
                part=base, source_file=source,
                params={'mode': 'equal', 'symmetric': True, 'axis': 'length'},
                approved=True, approved_by=self.manager,
            )
            record = resolve_cnc_file(dynamic, self.manager)
            self.assertEqual(record.kind, CncFile.KIND_DERIVED)
            self.assertEqual(record.source_file_id, source.pk)
            self.assertTrue(os.path.isfile(record.path))
            self.assertIn(".item", record.barcode_key)
            self.assertGreater(record.params.get('changed_lines', 0), 0)
            measured = record.length_after - record.length_before
            self.assertLess(abs(measured - Decimal("20")), Decimal("0.05"))
            again = resolve_cnc_file(dynamic, self.manager)
            self.assertEqual(again.pk, record.pk)

    def test_resolver_respects_derived_files_kill_switch(self):
        base, dynamic, source = self._dynamic_part_with_base_file()
        with self.settings(CNC_DERIVED_FILES_ENABLED=False):
            CncPartProfile.objects.create(
                part=base, source_file=source,
                params={'mode': 'equal', 'symmetric': True, 'axis': 'length'},
                approved=True, approved_by=self.manager,
            )
            with self.assertRaises(CncResolutionError):
                resolve_cnc_file(dynamic, self.manager)
        self.assertFalse(CncFile.objects.filter(kind=CncFile.KIND_DERIVED).exists())

    def test_controller_profile_is_not_ready_without_auditable_approval_and_fixture(self):
        profile = CncControllerProfile.objects.create(key="unknown-cnc", manufacturer="Unknown", model="Unknown")
        self.assertFalse(profile.governance_ready)
        self.assertFalse(profile.postprocessor_config["approved"])

    def test_same_length_part_uses_base_without_resize_profile(self):
        material = Material.objects.create(name="MDF same", thickness=Decimal("16.0"))
        base = Part.objects.create(
            material=material, name="Unchanged panel", length=Decimal("100.0"),
            width=Decimal("50.0"), f3="same.100x50", routing_code="cnc",
        )
        same_part = Part.objects.create(
            material=material, base_part=base, name=base.name,
            length=Decimal("100.0"), width=Decimal("50.0"),
            f3="same.100x50.item1", routing_code="cnc",
        )
        with tempfile.TemporaryDirectory() as archive:
            with self.settings(CNC_SOURCE_DIR=archive):
                with tempfile.NamedTemporaryFile(suffix=".cnc", delete=False) as stream:
                    with open(SAMPLE, "rb") as sample:
                        stream.write(sample.read())
                    source_path = stream.name
                try:
                    source = register_base_file(source_path, base.f3, self.manager)
                    record = resolve_cnc_file(same_part, self.manager)
                finally:
                    os.unlink(source_path)
        self.assertEqual(record.pk, source.pk)
        self.assertEqual(record.kind, CncFile.KIND_BASE)
