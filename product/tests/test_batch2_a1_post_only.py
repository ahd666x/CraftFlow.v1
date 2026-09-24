from django.test import TestCase
from django.contrib.auth.models import User, Group
from django.urls import reverse

from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    Customer, WorkerProfile, PackagingUnit,
)


class Batch2A1POSTOnlyTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', password='testpass')
        self.group1 = Group.objects.create(name='1')
        self.manager = User.objects.create_user('manager', password='testpass')
        self.manager.groups.add(self.group1)
        self.regular_user = User.objects.create_user('user1', password='testpass')
        self.packaging_user = User.objects.create_user('packer', password='testpass')
        WorkerProfile.objects.create(user=self.packaging_user, stage='packaging')

        self.category = ProductCategory.objects.create(name='دسته')
        self.product = Product.objects.create(category=self.category, name='محصول', base_price=1000)
        self.customer = Customer.objects.create(name='مشتری', phone='09120000000')

        self.order = Order.objects.create(user=self.admin, customer=self.customer, number='A1-1')
        self.order_item = OrderItem.objects.create(order=self.order, product=self.product, quantity=2)

        self.client.login(username='admin', password='testpass')

    def test_get_delete_all_tasks_returns_405(self):
        ProductionTask.objects.create(
            order=self.order, order_item=self.order_item,
            station_name='cut', step_order=1, quantity=2,
        )
        tasks_before = ProductionTask.objects.filter(order=self.order).count()
        response = self.client.get(reverse('delete_all_tasks', args=[self.order.id]))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(ProductionTask.objects.filter(order=self.order).count(), tasks_before)

    def test_get_delete_paint_tasks_returns_405(self):
        ProductionTask.objects.create(
            order_item=self.order_item,
            station_name='paint', step_order=1, quantity=2,
        )
        tasks_before = ProductionTask.objects.filter(order_item=self.order_item).count()
        response = self.client.get(reverse('delete_paint_tasks', args=[self.order_item.id]))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(ProductionTask.objects.filter(order_item=self.order_item).count(), tasks_before)

    def test_get_delete_all_paint_tasks_for_order_returns_405(self):
        ProductionTask.objects.create(
            order_item=self.order_item,
            station_name='paint', step_order=1, quantity=2,
        )
        tasks_before = ProductionTask.objects.filter(order_item=self.order_item).count()
        response = self.client.get(reverse('delete_all_paint_tasks_for_order', args=[self.order.id]))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(ProductionTask.objects.filter(order_item=self.order_item).count(), tasks_before)

    def test_get_customer_delete_order_item_returns_405(self):
        items_before = OrderItem.objects.filter(id=self.order_item.id).count()
        response = self.client.get(reverse('customer_delete_order_item', args=[self.order_item.id]))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(OrderItem.objects.filter(id=self.order_item.id).count(), items_before)

    def test_get_undo_packaging_unit_returns_405(self):
        self.client.login(username='packer', password='testpass')
        unit = PackagingUnit.objects.get(order_item=self.order_item, unit_number=1)
        units_before = PackagingUnit.objects.filter(id=unit.id).count()
        response = self.client.get(reverse('undo_packaging_unit', args=[unit.id]))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(PackagingUnit.objects.filter(id=unit.id).count(), units_before)

    def test_post_delete_all_tasks_works(self):
        ProductionTask.objects.create(
            order=self.order, order_item=self.order_item,
            station_name='cut', step_order=1, quantity=2,
        )
        response = self.client.post(reverse('delete_all_tasks', args=[self.order.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProductionTask.objects.filter(order=self.order).count(), 0)

    def test_post_customer_delete_order_item_works(self):
        response = self.client.post(reverse('customer_delete_order_item', args=[self.order_item.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(OrderItem.objects.filter(id=self.order_item.id).exists())

    def test_safe_next_blocks_external_url(self):
        unit = PackagingUnit.objects.get(order_item=self.order_item, unit_number=1)
        self.client.login(username='packer', password='testpass')
        url = reverse('scan_packaging_unit', args=[unit.id]) + '?next=https://evil.example/'
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('evil.example', response['Location'])

    def test_safe_next_allows_internal_path(self):
        unit = PackagingUnit.objects.get(order_item=self.order_item, unit_number=2)
        self.client.login(username='packer', password='testpass')
        url = reverse('scan_packaging_unit', args=[unit.id]) + '?next=/item/1/'
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/item/1/', response['Location'])
