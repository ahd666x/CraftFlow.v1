from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from inventory.models import MaterialIssue, RawMaterial, RawMaterialCategory


class Batch2C3QueueJSTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.raw_cat = RawMaterialCategory.objects.create(name='مواد')
        cls.raw = RawMaterial.objects.create(
            name='دسته', code='RM1', unit='kg', category=cls.raw_cat
        )

    def setUp(self):
        self.client.login(username='admin', password='testpass')

    def test_queue_page_uses_event_delegation(self):
        response = self.client.get(reverse('inventory:production_issue_queue'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("closest('.btn-cancel-issue')", html)
        self.assertNotIn("'.btn-cancel-issue').forEach", html)

    def test_cancel_issue_ajax_cancels_request(self):
        issue = MaterialIssue.objects.create(
            raw_material=self.raw,
            purpose='production',
            status='requested',
            requested_quantity=10,
            issued_quantity=0,
        )
        response = self.client.post(
            reverse('inventory:cancel_material_issue', args=[issue.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'cancelled')
