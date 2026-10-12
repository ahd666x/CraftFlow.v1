from django.test import TestCase
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from datetime import timedelta

from discounts.models import Discount


class DiscountAccessTests(TestCase):
    """پچ ۳: صفحات مدیریت تخفیف فقط برای staff قابل دسترسی است."""

    def setUp(self):
        self.regular = User.objects.create_user(
            username="regular", password="pw", is_staff=False,
        )
        self.staff = User.objects.create_user(
            username="admin", password="pw", is_staff=True,
        )
        self.discount = Discount.objects.create(
            code="TEST", discount_type="percent", value=10,
            max_uses=100,
            valid_from=timezone.now() - timedelta(days=1),
            valid_until=timezone.now() + timedelta(days=30),
        )
        self.client = Client()

    def _urls(self):
        return [
            '/discounts/',
            '/discounts/create/',
            f'/discounts/{self.discount.pk}/edit/',
        ]

    def test_anonymous_redirected_to_login(self):
        for url in self._urls():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn('/accounts/login', response.url)

    def test_regular_user_gets_403(self):
        self.client.login(username="regular", password="pw")
        for url in self._urls():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)

    def test_staff_user_gets_200(self):
        self.client.login(username="admin", password="pw")
        for url in self._urls():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)