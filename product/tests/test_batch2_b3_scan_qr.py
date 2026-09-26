from django.test import TestCase
from django.contrib.auth.models import User, Group
from django.urls import reverse
from django.db.models import Q

from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    Customer, WorkerProfile, ProductionLog,
)


class Batch2B3ScanQRTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.group1 = Group.objects.create(name='1')
        cls.cnc_worker = User.objects.create_user('cnc_worker', password='testpass')
        cls.cnc_worker.groups.add(cls.group1)
        WorkerProfile.objects.create(user=cls.cnc_worker, stage='cnc')

        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.other_category = ProductCategory.objects.create(name='دسته2')
        cls.other_product = Product.objects.create(category=cls.other_category, name='محصول2', base_price=2000)

    def setUp(self):
        self.client.login(username='cnc_worker', password='testpass')

    def _create_order(self, number='B3-1'):
        return Order.objects.create(user=self.admin, customer=self.customer, number=number)

    def test_no_pending_task_still_creates_log(self):
        """لاگ باید حتی بدون وجود تسک pending ایجاد شود"""
        order = self._create_order('B3-1')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        logs_before = ProductionLog.objects.count()
        response = self.client.post(reverse('scan_qr', args=[item.id]))
        self.assertEqual(response.status_code, 302)
        # لاگ باید ایجاد شده باشد
        self.assertEqual(ProductionLog.objects.count(), logs_before + 1)
        log = ProductionLog.objects.get(order_item=item, stage='cnc')
        self.assertEqual(log.user, self.cnc_worker)

    def test_repeat_scan_message_not_duplicate(self):
        order = self._create_order('B3-2')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        task = ProductionTask.objects.create(
            order=order, order_item=item,
            station_name='cnc', step_order=1, quantity=1, status='pending',
        )
        # First scan — should succeed and create a log
        self.client.post(reverse('scan_qr', args=[item.id]))
        logs_after_first = ProductionLog.objects.count()
        self.assertEqual(logs_after_first, 1)
        # Second scan — should not create another log
        response = self.client.post(reverse('scan_qr', args=[item.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProductionLog.objects.count(), logs_after_first)
        task.refresh_from_db()
        self.assertEqual(task.status, 'done')

    def test_does_not_pick_task_from_other_item(self):
        order = self._create_order('B3-3')
        item_a = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        item_b = OrderItem.objects.create(order=order, product=self.other_product, quantity=1)
        # Create a pending CNC task for item_b
        task_b = ProductionTask.objects.create(
            order=order, order_item=item_b,
            station_name='cnc', step_order=1, quantity=1, status='pending',
        )
        response = self.client.post(reverse('scan_qr', args=[item_a.id]))
        self.assertEqual(response.status_code, 302)
        # لاگ برای item_a ایجاد شده (مستقل از وجود تسک)
        self.assertEqual(ProductionLog.objects.filter(order_item=item_a).count(), 1)
        # تسک item_b نباید علامت‌گذاری شده باشد
        task_b.refresh_from_db()
        self.assertEqual(task_b.status, 'pending')

    def test_matching_pending_task_scanned(self):
        order = self._create_order('B3-4')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        task = ProductionTask.objects.create(
            order=order, order_item=item,
            station_name='cnc', step_order=1, quantity=1, status='pending',
        )
        response = self.client.post(reverse('scan_qr', args=[item.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProductionLog.objects.count(), 1)
        task.refresh_from_db()
        self.assertEqual(task.status, 'done')
