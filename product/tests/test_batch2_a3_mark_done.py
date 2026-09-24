from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    Customer, WorkerProfile,
)


class Batch2A3MarkTaskDoneTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.cnc_worker = User.objects.create_user('cnc_worker', password='testpass')
        WorkerProfile.objects.create(user=cls.cnc_worker, stage='cnc')
        cls.dr_worker = User.objects.create_user('dr_worker', password='testpass')
        WorkerProfile.objects.create(user=cls.dr_worker, stage='dr')
        cls.no_profile_user = User.objects.create_user('no_profile', password='testpass')

        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='A3-1')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)

    def test_cnc_worker_cnc_pending_task_success(self):
        self.client.login(username='cnc_worker', password='testpass')
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item,
            station_name='cnc', step_order=1, quantity=2, status='pending',
        )
        response = self.client.post(reverse('mark_task_done', args=[task.id]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        task.refresh_from_db()
        self.assertEqual(task.completed_quantity, 1)

    def test_task_waiting_status_returns_400(self):
        self.client.login(username='cnc_worker', password='testpass')
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item,
            station_name='cnc', step_order=1, quantity=2, status='waiting',
        )
        response = self.client.post(reverse('mark_task_done', args=[task.id]))
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])

    def test_wrong_station_returns_403(self):
        self.client.login(username='cnc_worker', password='testpass')
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item,
            station_name='dr', step_order=1, quantity=2, status='pending',
        )
        response = self.client.post(reverse('mark_task_done', args=[task.id]))
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertFalse(data['success'])

    def test_task_done_returns_400(self):
        self.client.login(username='cnc_worker', password='testpass')
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item,
            station_name='cnc', step_order=1, quantity=2, status='done',
        )
        response = self.client.post(reverse('mark_task_done', args=[task.id]))
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])

    def test_user_without_profile_returns_403(self):
        self.client.login(username='no_profile', password='testpass')
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item,
            station_name='cnc', step_order=1, quantity=2, status='pending',
        )
        response = self.client.post(reverse('mark_task_done', args=[task.id]))
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertFalse(data['success'])

    def test_superuser_can_mark_any_station(self):
        self.client.login(username='admin', password='testpass')
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item,
            station_name='dr', step_order=1, quantity=2, status='pending',
        )
        response = self.client.post(reverse('mark_task_done', args=[task.id]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
