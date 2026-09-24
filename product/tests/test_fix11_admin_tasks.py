from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    ProductionEvent, Customer, Color, PaintingProcess, PaintingStage,
)


class AdminTasksTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('atadmin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='AT1')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        cls.process = PaintingProcess.objects.create(name='روند', code='AT', color_codes=['8'], is_active=True)
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='دهان', duration_minutes=30,
            drying_time_minutes=60, required_skill='painter',
        )

    def setUp(self):
        self.client.login(username='atadmin', password='testpass')

    def _make_task(self, status='pending'):
        return ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status=status, painting_stage=self.stage, color_part='بدنه',
            step_order=1,
        )

    def test_page_has_no_nested_form(self):
        self._make_task()
        response = self.client.get(reverse('admin_tasks_management'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'this.form.submit()', response.content)

    def test_single_status_changes_task(self):
        task = self._make_task(status='done')
        response = self.client.post(reverse('admin_tasks_management'), {
            'action': 'single_status', 'task_id': task.id, 'status': 'pending',
        })
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'pending')
        self.assertEqual(task.completed_quantity, 0)

    def test_bulk_status_done(self):
        t1 = self._make_task(status='pending')
        t2 = self._make_task(status='pending')
        response = self.client.post(reverse('admin_tasks_management'), {
            'action': 'bulk_status', 'task_ids': [t1.id, t2.id], 'bulk_status': 'done',
        })
        self.assertEqual(response.status_code, 302)
        for t in (t1, t2):
            t.refresh_from_db()
            self.assertEqual(t.status, 'done')

    def test_bulk_worker(self):
        worker = User.objects.create_user('w', password='testpass')
        t1 = self._make_task(status='pending')
        response = self.client.post(reverse('admin_tasks_management'), {
            'action': 'bulk_worker', 'task_ids': [t1.id], 'bulk_worker': worker.id,
        })
        self.assertEqual(response.status_code, 302)
        t1.refresh_from_db()
        self.assertEqual(t1.assigned_worker_id, worker.id)

    def test_bulk_delete(self):
        t1 = self._make_task(status='pending')
        response = self.client.post(reverse('admin_tasks_management'), {
            'action': 'bulk_delete', 'task_ids': [t1.id],
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProductionTask.objects.filter(pk=t1.id).exists())

    def test_bulk_status_done_single_done_event(self):
        t1 = self._make_task(status='pending')
        response = self.client.post(reverse('admin_tasks_management'), {
            'action': 'bulk_status', 'task_ids': [t1.id], 'bulk_status': 'done',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProductionEvent.objects.filter(task=t1, event_type='done').count(), 1)