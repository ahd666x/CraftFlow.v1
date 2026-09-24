from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse, NoReverseMatch
from django.test import Client


class ExcelExportImportRemovedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('exadmin', password='testpass')

    def test_export_url_no_reverse(self):
        with self.assertRaises(NoReverseMatch):
            reverse('export_all_data')

    def test_import_url_no_reverse(self):
        with self.assertRaises(NoReverseMatch):
            reverse('import_data')

    def test_paths_404(self):
        client = Client()
        client.login(username='exadmin', password='testpass')
        for path in ('/export-all/', '/import-data/'):
            response = client.get(path)
            self.assertEqual(response.status_code, 404, path)