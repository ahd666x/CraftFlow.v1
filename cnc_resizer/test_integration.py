import os
import tempfile

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from .models import CncFile
from .services import canonical_barcode_key, register_base_file
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
        self.assertEqual(response.status_code, 302)

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
