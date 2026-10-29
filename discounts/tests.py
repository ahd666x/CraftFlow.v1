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

class GuestDiscountTests(TestCase):
    """ممیزی دور ۶: اعمال/حذف کد تخفیف برای سبد مهمان هم مجاز است.

    سبد مهمان با session_key کار می‌کند و بعد از ورود merge می‌شود؛
    LoginRequiredMixin قبلاً مهمان را وسط راه به صفحهٔ ورود پرت می‌کرد.
    """

    def setUp(self):
        self.discount = Discount.objects.create(
            code="GUEST", discount_type="percent", value=10,
            max_uses=100,
            valid_from=timezone.now() - timedelta(days=1),
            valid_until=timezone.now() + timedelta(days=30),
        )
        self.client = Client()

    def test_guest_can_apply_discount(self):
        response = self.client.post('/discounts/apply/', {'code': 'GUEST'})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('/accounts/login', response.url)

    def test_guest_can_remove_discount(self):
        response = self.client.post('/discounts/remove/', {'code': 'GUEST'})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('/accounts/login', response.url)

    def test_guest_hx_apply_returns_partial(self):
        response = self.client.post(
            '/discounts/apply/', {'code': 'GUEST'},
            HTTP_HX_REQUEST='true', HTTP_HX_TARGET='cart-rows',
        )
        self.assertEqual(response.status_code, 200)
        # هدر با مقدار غیر ASCII به‌صورت RFC2047 انکود می‌شود؛ اول دیکودش می‌کنیم
        from email.header import decode_header, make_header
        trigger = str(make_header(decode_header(response['HX-Trigger'])))
        self.assertIn('cartMessage', trigger)
        self.assertIn('GUEST', trigger)
