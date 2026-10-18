import os
import tempfile

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from decimal import Decimal

from product.models import Material, Part, Product, ProductBOM, ProductCategory

from .models import CncFile, CncPartProfile
from .services import canonical_barcode_key, register_base_file, resolve_cnc_file
from .tests import SAMPLE


class CncIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("cnc-manager", password="testpass")
        cls.manager.groups.add(Group.objects.create(name="2"))
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

    def test_resolver_uses_length_as_cnc_y_and_caches_derived_file(self):
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
        with tempfile.TemporaryDirectory() as archive:
            with self.settings(CNC_SOURCE_DIR=archive):
                with tempfile.NamedTemporaryFile(suffix=".cnc", delete=False) as stream:
                    with open(SAMPLE, "rb") as sample:
                        stream.write(sample.read())
                    source_path = stream.name
                try:
                    source = register_base_file(source_path, base.f3, self.manager)
                    CncPartProfile.objects.create(
                        part=base, source_file=source,
                        params={'mode': 'equal', 'symmetric': True, 'axis': 'length'},
                        approved=True, approved_by=self.manager,
                    )
                    record = resolve_cnc_file(dynamic, self.manager)
                    derived_exists = os.path.exists(record.path)
                finally:
                    os.unlink(source_path)
        self.assertEqual(record.kind, CncFile.KIND_DERIVED)
        self.assertTrue(record.path.endswith(".cnc"))
        self.assertTrue(derived_exists)
        self.assertAlmostEqual(float(record.params["delta"]), 20.0, places=3)

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
