import json
from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import WorkerProfile
from product.views import worker_to_dict


class Batch2C2WorkerDictTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.worker_no_full_name = User.objects.create_user('worker1', password='testpass')
        WorkerProfile.objects.create(user=cls.worker_no_full_name, stage='paint')
        cls.worker_with_full_name = User.objects.create_user('worker2', password='testpass',
                                                             first_name='علی', last_name='نژاد')
        WorkerProfile.objects.create(user=cls.worker_with_full_name, stage='paint')

    def setUp(self):
        self.client.login(username='admin', password='testpass')

    def test_worker_to_dict_has_display_name(self):
        wp = WorkerProfile.objects.get(user__username='worker2')
        d = worker_to_dict(wp)
        self.assertEqual(d['display_name'], 'علی نژاد')

    def test_worker_to_dict_display_name_fallback(self):
        wp = WorkerProfile.objects.get(user__username='worker1')
        d = worker_to_dict(wp)
        self.assertEqual(d['display_name'], 'worker1')

    def test_painting_workers_api_returns_display_name(self):
        # Create a new user (not already a worker)
        new_user = User.objects.create_user('new_worker', password='testpass',
                                             first_name='حسن', last_name='علی')
        response = self.client.post(
            reverse('painting_workers_api'),
            data=json.dumps({
                'user_id': new_user.id,
                'stage': 'paint',
                'is_available': True,
                'skills': ['painter'],
                'skill_priority': {},
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertIn('display_name', data['worker'])
        self.assertEqual(data['worker']['display_name'], 'حسن علی')
        self.assertIn('skills', data['worker'])
