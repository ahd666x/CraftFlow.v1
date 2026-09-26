from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import (
    ProductCategory, Product, Order, OrderItem, Customer,
    PackagingUnit,
)


class Batch2C4TemplateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.user = User.objects.create_user('cust_user', password='testpass')

    def setUp(self):
        self.client.login(username='admin', password='testpass')

    def test_delivery_confirm_has_packed_and_total_units(self):
        order = Order.objects.create(
            user=self.user, customer=self.customer, number='C4-1')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=3)
        units = list(item.packaging_units.all())
        for u in units:
            u.is_packed = True
            u.is_shipped = False
            u.save()
        response = self.client.get(reverse('delivery_confirm', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('3 بسته‌بندی‌شده از 3', html)

    def test_scan_packaging_unit_undo_button_packed_by(self):
        other_user = User.objects.create_user('other_user', password='testpass')
        order = Order.objects.create(
            user=self.user, customer=self.customer, number='C4-2')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        unit = item.packaging_units.first()
        unit.is_packed = True
        unit.packed_by = other_user
        unit.is_shipped = False
        unit.save()
        self.client.login(username='other_user', password='testpass')
        response = self.client.get(reverse('scan_packaging_unit', args=[unit.id]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('لغو بسته‌بندی', html)

    def test_scan_packaging_unit_no_undo_button_for_non_author_non_superuser(self):
        packing_user = User.objects.create_user('packing_user', password='testpass')
        order = Order.objects.create(
            user=self.user, customer=self.customer, number='C4-3')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        unit = item.packaging_units.first()
        unit.is_packed = True
        unit.packed_by = packing_user
        unit.is_shipped = False
        unit.save()
        viewer = User.objects.create_user('viewer', password='testpass')
        self.client.login(username='viewer', password='testpass')
        response = self.client.get(reverse('scan_packaging_unit', args=[unit.id]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn('لغو بسته‌بندی', html)
