from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.core.management import call_command
from io import StringIO

from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    ProductionEvent, Customer, Color, PaintingProcess, PaintingStage,
)


class TaskRevertTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('rvadmin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='RV1')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        cls.process = PaintingProcess.objects.create(name='روند', code='RV', color_codes=['8'], is_active=True)
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='دهان', duration_minutes=30,
            drying_time_minutes=60, required_skill='painter',
        )

    def setUp(self):
        self.client.login(username='rvadmin', password='testpass')

    def test_revert_done_to_pending(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='done', painting_stage=self.stage, color_part='بدنه',
            step_order=1, completed_quantity=2,
        )
        task.status = 'pending'
        task.save()
        task.refresh_from_db()
        self.assertEqual(task.status, 'pending')
        self.assertEqual(task.completed_quantity, 0)
        self.assertIsNone(task.completed_at)

    def test_revert_then_save_does_not_redo(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='done', painting_stage=self.stage, color_part='بدنه',
            step_order=1, completed_quantity=2,
        )
        task.status = 'pending'
        task.save()
        # Re-save without changing completed_quantity (which is now 0) → stays pending
        task.save()
        task.refresh_from_db()
        self.assertEqual(task.status, 'pending')
        self.assertEqual(task.completed_quantity, 0)

    def test_increasing_completed_quantity_makes_done(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='pending', painting_stage=self.stage, color_part='بدنه',
            step_order=1,
        )
        task.completed_quantity = 2
        task.save()
        task.refresh_from_db()
        self.assertEqual(task.status, 'done')

    def test_admin_action_save_tasks_pending(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='done', painting_stage=self.stage, color_part='بدنه',
            step_order=1, completed_quantity=2,
        )
        # admin action: bulk_status with new_status=pending
        response = self.client.post(reverse('admin_tasks_management'), {
            'action': 'bulk_status',
            'task_ids': [task.id],
            'bulk_status': 'pending',
        })
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'pending')

    def test_bulk_status_done_records_single_done_event(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='pending', painting_stage=self.stage, color_part='بدنه',
            step_order=1,
        )
        response = self.client.post(reverse('admin_tasks_management'), {
            'action': 'bulk_status',
            'task_ids': [task.id],
            'bulk_status': 'done',
        })
        self.assertEqual(response.status_code, 302)
        done_events = ProductionEvent.objects.filter(task=task, event_type='done')
        self.assertEqual(done_events.count(), 1)