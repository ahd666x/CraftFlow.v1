from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import Product, ProductCategory, Order, OrderItem, Color, Customer


class Batch2B2AdminEditItemTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product_a = Product.objects.create(
            category=cls.category, name='محصول A', base_price=1000,
            default_size='50', price_increment_per_cm=0,
        )
        cls.product_b = Product.objects.create(
            category=cls.category, name='محصول B', base_price=2000,
            default_size='50', price_increment_per_cm=0,
        )
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')

    def setUp(self):
        self.client.login(username='admin', password='testpass')

    def _create_order_item(self):
        order = Order.objects.create(user=self.admin, customer=self.customer, number='B2-1')
        return OrderItem.objects.create(order=order, product=self.product_a, quantity=1, size='50')

    def _color_data(self):
        data = {}
        for part_value, _ in Color.PART_CHOICES:
            data[f'color_{part_value}'] = ''
        return data

    def test_change_product_updates_price(self):
        item = self._create_order_item()
        data = {
            'category': self.product_b.category.id,
            'product': self.product_b.id,
            'quantity': 1,
            'size': '50',
            'notes': '',
            'unit_price': '',
        }
        data.update(self._color_data())
        response = self.client.post(reverse('admin_edit_order_item', args=[item.id]), data)
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.product_id, self.product_b.id)
        expected_price = item.calculate_price()
        self.assertEqual(item.unit_price, expected_price)

    def test_change_unit_price_preserved(self):
        item = self._create_order_item()
        item.unit_price = 5000
        item._skip_price_calc = True
        item.save()
        data = {
            'category': self.product_a.category.id,
            'product': self.product_a.id,
            'quantity': 1,
            'size': '50',
            'notes': '',
            'unit_price': '5000',
        }
        data.update(self._color_data())
        response = self.client.post(reverse('admin_edit_order_item', args=[item.id]), data)
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.unit_price, 5000)

    def test_change_only_notes_preserves_manual_price(self):
        item = self._create_order_item()
        item.unit_price = 5000
        item._skip_price_calc = True
        item.save()
        data = {
            'category': self.product_a.category.id,
            'product': self.product_a.id,
            'quantity': 1,
            'size': '50',
            'notes': 'یادداشت جدید',
            'unit_price': '',
        }
        data.update(self._color_data())
        response = self.client.post(reverse('admin_edit_order_item', args=[item.id]), data)
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.unit_price, 5000)

    def test_change_size_recalculates_price(self):
        product_with_increment = Product.objects.create(
            category=self.category, name='محصول C', base_price=1000,
            default_size='50', price_increment_per_cm=10,
        )
        order = Order.objects.create(user=self.admin, customer=self.customer, number='B2-2')
        item = OrderItem.objects.create(
            order=order, product=product_with_increment, quantity=1, size='50'
        )
        data = {
            'category': product_with_increment.category.id,
            'product': product_with_increment.id,
            'quantity': 1,
            'size': '100',
            'notes': '',
            'unit_price': '',
        }
        data.update(self._color_data())
        response = self.client.post(reverse('admin_edit_order_item', args=[item.id]), data)
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        expected_price = item.calculate_price()
        self.assertEqual(item.unit_price, expected_price)
        self.assertNotEqual(item.unit_price, 1000)  # price should differ due to size change
