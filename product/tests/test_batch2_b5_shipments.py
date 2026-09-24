from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import Product, ProductCategory, Order, OrderItem, Customer


class Batch2B5ShipmentsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_superuser('admin', password='testpass')
        cls.customer_obj = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.user = User.objects.create_user('cust_user', password='testpass')

    def setUp(self):
        self.client.login(username='cust_user', password='testpass')

    def test_customer_shipments_renders(self):
        order = Order.objects.create(
            user=self.user, customer=self.customer_obj, number='B5-1')
        OrderItem.objects.create(order=order, product=self.product, quantity=1)
        response = self.client.get(reverse('customer_shipments'))
        self.assertEqual(response.status_code, 200)

    def test_shipment_detail_plate_with_slash(self):
        order = Order.objects.create(
            user=self.user, customer=self.customer_obj, number='B5-2')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        url = reverse('customer_shipment_detail', args=['12/34', '2024-01-01'])
        response = self.client.get(url)
        # Should redirect (no matching shipments) rather than 404 on URL parse
        self.assertEqual(response.status_code, 302)

    def test_delayed_orders_uses_select_related(self):
        from django.db import connection
        with self.assertNumQueries(3) as _:
            self.client.get(reverse('report_delayed'))
        # Should not raise N+1 on customer access

    def test_delivery_note_template_has_shamsi_date(self):
        from django.template.loader import render_to_string
        order = Order.objects.create(
            user=self.user, customer=self.customer_obj, number='B5-3')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        html = render_to_string(
            'reports/delivery_note.html',
            {
                'units': [item],
                'plate': '12-34',
                'date': '2024-01-01',
                'shamsi_date': '1399/01/01',
                'representative_name': 'نماینده',
                'customer_name': 'مشتری',
                'total_price': 1000,
            },
        )
        self.assertIn('1399/01/01', html)
        self.assertNotIn('today', html)
