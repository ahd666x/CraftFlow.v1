from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import WorkerProfile


class WorkerSkillFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('wsadmin', password='testpass')
        cls.u1 = User.objects.create_user('w1', password='testpass')
        cls.u2 = User.objects.create_user('w2', password='testpass')
        cls.u3 = User.objects.create_user('w3', password='testpass')
        WorkerProfile.objects.create(user=cls.u1, stage='paint', skills=['painter'])
        WorkerProfile.objects.create(user=cls.u2, stage='paint', skills=['general'])
        WorkerProfile.objects.create(user=cls.u3, stage='paint', skills=['painter', 'general'])

    def setUp(self):
        self.client.login(username='wsadmin', password='testpass')

    def test_api_skill_filter_painter(self):
        response = self.client.get(reverse('painting_workers_api'), {'skill': 'painter'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['total'], 2)

    def test_view_skill_filter_general(self):
        response = self.client.get(reverse('painting_workers'), {'skill': 'general'})
        self.assertEqual(response.status_code, 200)