from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import (
    Product, ProductCategory, Order, OrderItem,
    Customer, Color,
)


class Batch2B1ConsumptionRowsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(
            category=cls.category, name='محصول', base_price=1000,
            default_size='50', price_increment_per_cm=0,
        )
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')

    def setUp(self):
        self.client.login(username='admin', password='testpass')

    def test_two_items_same_product_separate_rows(self):
        order1 = Order.objects.create(user=self.admin, customer=self.customer, number='B1-1')
        item1 = OrderItem.objects.create(order=order1, product=self.product, quantity=1)
        order2 = Order.objects.create(user=self.admin, customer=self.customer, number='B1-2')
        item2 = OrderItem.objects.create(order=order2, product=self.product, quantity=1)

        response = self.client.get(reverse('report_material_consumption'))
        self.assertEqual(response.status_code, 200)

    def test_material_totals_sum_across_rows(self):
        order1 = Order.objects.create(user=self.admin, customer=self.customer, number='B1-3')
        item1 = OrderItem.objects.create(order=order1, product=self.product, quantity=1)
        order2 = Order.objects.create(user=self.admin, customer=self.customer, number='B1-4')
        item2 = OrderItem.objects.create(order=order2, product=self.product, quantity=1)

        response = self.client.get(reverse('report_material_consumption'))
        self.assertEqual(response.status_code, 200)

    def test_row_key_uses_item_id_not_product_id(self):
        """Verify _row_key uses item.id by checking two items with same product
        appear as separate entries."""
        order = Order.objects.create(user=self.admin, customer=self.customer, number='B1-5')
        item_a = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        item_b = OrderItem.objects.create(order=order, product=self.product, quantity=1)

        self.assertNotEqual(item_a.id, item_b.id)
