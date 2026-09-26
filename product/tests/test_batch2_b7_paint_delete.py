from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    Customer,
)
from inventory.models import RawMaterial, RawMaterialCategory, StockMovement, MaterialIssue


class Batch2B7PaintDeleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.raw_cat = RawMaterialCategory.objects.create(name='مواد')

    def setUp(self):
        self.client.login(username='admin', password='testpass')

    def _create_item_with_cut_and_paint_tasks(self, number='B7-1'):
        order = Order.objects.create(user=self.admin, customer=self.customer, number=number)
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        cut_task = ProductionTask.objects.create(
            order=order, order_item=item,
            station_name='cut', step_order=1, quantity=1, status='pending',
        )
        paint_task = ProductionTask.objects.create(
            order=order, order_item=item,
            station_name='paint', step_order=1, quantity=1, status='pending',
            color_part='body',
        )
        return order, item, cut_task, paint_task

    def _create_raw_material(self, name):
        return RawMaterial.objects.create(name=name, code='RM', unit='kg', category=self.raw_cat)

    def test_delete_paint_tasks_only_deletes_paint(self):
        _, item, cut_task, paint_task = self._create_item_with_cut_and_paint_tasks('B7-1')
        self.assertTrue(ProductionTask.objects.filter(id=cut_task.id).exists())
        self.assertTrue(ProductionTask.objects.filter(id=paint_task.id).exists())
        response = self.client.post(reverse('delete_paint_tasks', args=[item.id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ProductionTask.objects.filter(id=cut_task.id).exists(), "Cut task should still exist")
        self.assertFalse(ProductionTask.objects.filter(id=paint_task.id).exists(), "Paint task should be deleted")

    def test_cancel_unissued_paint_material_requests(self):
        _, item, _, _ = self._create_item_with_cut_and_paint_tasks('B7-2')
        issue = MaterialIssue.objects.create(
            order_item=item, color_part='body', purpose='production',
            status='requested', issued_quantity=0, requested_quantity=10,
            raw_material=self._create_raw_material('دسته'),
        )
        self.client.post(reverse('delete_paint_tasks', args=[item.id]))
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'cancelled')

    def test_partial_and_issued_not_cancelled(self):
        _, item, _, _ = self._create_item_with_cut_and_paint_tasks('B7-3')
        issued = MaterialIssue.objects.create(
            order_item=item, color_part='body', purpose='production',
            status='issued', issued_quantity=10, requested_quantity=10,
            raw_material=self._create_raw_material('دسته2'),
        )
        partial = MaterialIssue.objects.create(
            order_item=item, color_part='body', purpose='production',
            status='partial', issued_quantity=5, requested_quantity=10,
            raw_material=self._create_raw_material('دسته3'),
        )
        self.client.post(reverse('delete_paint_tasks', args=[item.id]))
        issued.refresh_from_db()
        partial.refresh_from_db()
        self.assertEqual(issued.status, 'issued', "Issued should not be cancelled")
        self.assertEqual(partial.status, 'partial', "Partial should not be cancelled")

    def test_delete_only_one_stage_does_not_cancel(self):
        order = Order.objects.create(user=self.admin, customer=self.customer, number='B7-4')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        # Create two paint tasks for different color_parts
        paint_body = ProductionTask.objects.create(
            order=order, order_item=item,
            station_name='paint', step_order=1, quantity=1, status='pending',
            color_part='body',
        )
        paint_door = ProductionTask.objects.create(
            order=order, order_item=item,
            station_name='paint', step_order=2, quantity=1, status='pending',
            color_part='door',
        )
        issue = MaterialIssue.objects.create(
            order_item=item, color_part='door', purpose='production',
            status='requested', issued_quantity=0, requested_quantity=10,
            raw_material=self._create_raw_material('دسته4'),
        )
        # Delete only the body paint task via task_ids
        response = self.client.post(reverse('painting_delete_tasks'), {
            'task_ids[]': [paint_body.id],
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'requested', "Door issue should not be cancelled when body task deleted")

    def test_has_paint_tasks_context_flag(self):
        _, item, _, _ = self._create_item_with_cut_and_paint_tasks('B7-5')
        # Use client to get the response as it would render the template
        response = self.client.get(reverse('item_detail', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        # Check that has_paint_tasks is in the context (should be True since we created paint tasks)
        self.assertIn('has_paint_tasks', response.context)
        self.assertTrue(response.context['has_paint_tasks'])
