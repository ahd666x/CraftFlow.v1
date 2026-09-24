from django.test import TestCase
from django.contrib.auth.models import Group, User
from django.urls import reverse


class WarehouseDecoratorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('admin', password='testpass')
        cls.warehouse_group = Group.objects.get_or_create(name='انبار')[0]
        cls.manager_group = Group.objects.get_or_create(name='1')[0]
        cls.plain_user = User.objects.create_user('plain', password='testpass')

    def setUp(self):
        self.client.login(username='plain', password='testpass')

    def test_unauthorized_redirects_to_customer_orders(self):
        self.client.login(username='plain', password='testpass')
        response = self.client.get(reverse('inventory:production_issue_queue'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/customer/orders/')

    def test_unauthorized_ajax_returns_403_json(self):
        self.client.login(username='plain', password='testpass')
        response = self.client.post(
            reverse('inventory:handover_preview'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('دسترسی', data['error'])

    def test_warehouse_user_can_access(self):
        self.client.login(username='plain', password='testpass')
        self.plain_user.groups.add(self.warehouse_group)
        response = self.client.get(reverse('inventory:production_issue_queue'))
        self.assertEqual(response.status_code, 200)

    def test_manager_group_can_access(self):
        self.client.login(username='plain', password='testpass')
        self.plain_user.groups.add(self.manager_group)
        response = self.client.get(reverse('inventory:production_issue_queue'))
        self.assertEqual(response.status_code, 200)

    def test_superuser_can_access(self):
        self.client.login(username='admin', password='testpass')
        response = self.client.get(reverse('inventory:production_issue_queue'))
        self.assertEqual(response.status_code, 200)