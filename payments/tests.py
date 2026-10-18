from django.test import TestCase
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from datetime import timedelta

from product.models import Customer, Order, Product, ProductCategory
from payments.models import Payment


class CodPaymentTests(TestCase):
    """پچ ۹: پرداخت درب_home (COD) نباید سفارش را paid کند."""

    def setUp(self):
        self.user = User.objects.create_user(username="buyer", password="pw")
        self.customer = Customer.objects.create(name="خریدار")
        self.category = ProductCategory.objects.create(name="دسته")
        self.product = Product.objects.create(
            category=self.category, name="کالا", base_price=1000,
        )
        self.order = Order.objects.create(
            user=self.user, customer=self.customer, status='draft',
            total_amount=1000, final_amount=1000,
        )
        self.payment = Payment.objects.create(
            order=self.order, amount=1000, gateway='cash_on_delivery',
        )
        self.client = Client()
        self.client.login(username="buyer", password="pw")

    def test_cod_verify_does_not_mark_order_paid(self):
        response = self.client.get('/payments/verify/', {
            'gateway': 'cash_on_delivery', 'payment_id': self.payment.id,
        })
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.order.status, 'draft')
        self.assertEqual(self.payment.status, 'pending')
        self.assertIsNone(self.order.paid_at)