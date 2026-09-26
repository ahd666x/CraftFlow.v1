from django.test import TestCase
from django.contrib.auth.models import User, Group
from django.urls import reverse

from product.models import (
    ProductCategory, Product, Order, OrderItem, ProductionTask,
    Customer, WorkerProfile, PaintingAssignmentRule,
    PaintingStage, PaintingProcess,
)


class Batch2C1RulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')

        cls.group1 = Group.objects.create(name='1')
        cls.worker1 = User.objects.create_user('worker1', password='testpass')
        cls.worker1.groups.add(cls.group1)
        cls.worker_profile = WorkerProfile.objects.create(user=cls.worker1, stage='paint')
        cls.process = PaintingProcess.objects.create(name='فرآیند 1')
        cls.stage = PaintingStage.objects.create(
            process=cls.process, name='مرحله 1', order=1, duration_minutes=30
        )

    def setUp(self):
        self.client.login(username='admin', password='testpass')

    def test_rule_with_null_color_codes_no_none_in_html(self):
        PaintingAssignmentRule.objects.create(
            worker=self.worker_profile,
            painting_stage=None,
            process=None,
            color_codes=None,
            rule_type='priority',
            priority=100,
        )
        response = self.client.get(reverse('painting_assignment_rules'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('data-colors="None"', response.content.decode())

    def test_create_with_priority(self):
        response = self.client.post(
            reverse('painting_assignment_rules'),
            {
                'action': 'create',
                'worker': self.worker_profile.id,
                'stage': '',
                'process': '',
                'color_codes': '',
                'rule_type': 'priority',
                'priority': '250',
                'is_active': 'true',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        rule = PaintingAssignmentRule.objects.get(id=data['id'])
        self.assertEqual(rule.priority, 250)

    def test_edit_with_priority(self):
        rule = PaintingAssignmentRule.objects.create(
            worker=self.worker_profile,
            painting_stage=None,
            process=None,
            color_codes=None,
            rule_type='priority',
            priority=100,
        )
        response = self.client.post(
            reverse('painting_assignment_rules'),
            {
                'action': 'edit',
                'rule_id': rule.id,
                'worker': self.worker_profile.id,
                'stage': '',
                'process': '',
                'color_codes': '',
                'rule_type': 'priority',
                'priority': '300',
                'is_active': 'true',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        rule.refresh_from_db()
        self.assertEqual(rule.priority, 300)

    def test_create_with_invalid_priority_falls_back_to_100(self):
        response = self.client.post(
            reverse('painting_assignment_rules'),
            {
                'action': 'create',
                'worker': self.worker_profile.id,
                'stage': '',
                'process': '',
                'color_codes': '',
                'rule_type': 'priority',
                'priority': 'not-a-number',
                'is_active': 'true',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        rule = PaintingAssignmentRule.objects.get(id=data['id'])
        self.assertEqual(rule.priority, 100)
