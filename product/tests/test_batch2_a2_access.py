from django.test import TestCase
from django.contrib.auth.models import User, Group
from django.urls import reverse

from product.models import (
    Product, ProductCategory, Order, OrderItem,
    Customer, Color,
)


class Batch2A2AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.group1 = Group.objects.create(name='1')
        cls.manager = User.objects.create_user('manager', password='testpass')
        cls.manager.groups.add(cls.group1)
        cls.regular_user = User.objects.create_user('regular', password='testpass')
        # r10: ajax_load_product_colors توسط پنل نماینده هم استفاده می‌شود
        # (گروه «نماینده» با مایگریشن accounts/0002 از قبل هست → get_or_create)
        cls.rep_group, _ = Group.objects.get_or_create(name='نماینده')
        cls.rep = User.objects.create_user('rep', password='testpass')
        cls.rep.groups.add(cls.rep_group)

        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='A2-1')
        OrderItem.objects.create(order=cls.order, product=cls.product, quantity=1)

    def test_order_combined_print_anonymous_redirects(self):
        url = reverse('order_combined_print', args=[self.order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_order_combined_print_regular_user_redirects(self):
        self.client.login(username='regular', password='testpass')
        url = reverse('order_combined_print', args=[self.order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_order_combined_print_group1_user_ok(self):
        self.client.login(username='manager', password='testpass')
        url = reverse('order_combined_print', args=[self.order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_order_combined_print_admin_ok(self):
        self.client.login(username='admin', password='testpass')
        url = reverse('order_combined_print', args=[self.order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_ajax_load_product_colors_anonymous_redirects(self):
        url = reverse('ajax_load_product_colors', args=[self.product.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_ajax_load_product_colors_regular_user_blocked(self):
        # r10: کاربر عادی فروشگاه از AJAXهای داخلی بلاک است
        self.client.login(username='regular', password='testpass')
        url = reverse('ajax_load_product_colors', args=[self.product.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_ajax_load_product_colors_representative_ok(self):
        # r10: پنل نماینده به این AJAX نیاز دارد
        self.client.login(username='rep', password='testpass')
        url = reverse('ajax_load_product_colors', args=[self.product.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_ajax_load_product_colors_group1_and_admin_ok(self):
        self.client.login(username='manager', password='testpass')
        url = reverse('ajax_load_product_colors', args=[self.product.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.login(username='admin', password='testpass')
        self.assertEqual(self.client.get(url).status_code, 200)
