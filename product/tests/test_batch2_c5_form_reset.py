from django.test import TestCase
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.urls import reverse

from product.models import (
    ProductCategory, Product, Order, OrderItem, Customer,
    PackagingUnit,
)


class Batch2C5FormResetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')

    def test_create_order_has_reset_script(self):
        self.client.login(username='admin', password='testpass')
        response = self.client.get(reverse('create_order'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('reset', html)

    def test_color_filter_js_has_escapeHtml(self):
        import os
        import product
        js_path = os.path.join(
            os.path.dirname(product.__file__),
            'static', 'js', 'color_filter.js'
        )
        with open(js_path, encoding='utf-8') as f:
            js_content = f.read()
        self.assertIn('escapeHtml', js_content)
