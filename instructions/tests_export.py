"""تست‌های خروجی Word، مخفی‌سازی بر اساس نوع و دسترسی سند سفارش.

برای اجرا: نام این فایل را به instructions/tests_export.py نگه دارید
(یا محتوایش را به tests.py اضافه کنید) و ``python manage.py test instructions``.
"""
import io
import zipfile

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from instructions.models import WorkInstruction
from instructions.tests import _base_data


def _docx_text(response):
    body = b''.join(response.streaming_content)
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return zf.read('word/document.xml').decode('utf-8')


class DocxExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _base_data(cls)
        cls.staff = User.objects.create_user('exp_staff', password='x', is_staff=True)
        WorkInstruction.objects.create(product=cls.product, station='cnc', kind='step', title='مرحله اکسپورت')
        WorkInstruction.objects.create(product=cls.product, station='pvc', kind='note', title='نکته اکسپورت')
        WorkInstruction.objects.create(product=cls.product, station='shipping', kind='note', title='بند ارسال')

    def test_product_docx_downloads_and_contains_instructions(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('instructions:product_doc_docx', args=[self.product.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('wordprocessingml', resp['Content-Type'])
        xml = _docx_text(resp)
        self.assertIn('مرحله اکسپورت', xml)
        self.assertIn('نکته اکسپورت', xml)

    def test_hide_param_removes_kind_from_docx(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('instructions:product_doc_docx', args=[self.product.id]), {'hide': 'note,bogus'})
        xml = _docx_text(resp)
        self.assertIn('مرحله اکسپورت', xml)
        self.assertNotIn('نکته اکسپورت', xml)

    def test_docx_requires_staff(self):
        plain = User.objects.create_user('exp_plain', password='x')
        self.client.force_login(plain)
        resp = self.client.get(reverse('instructions:product_doc_docx', args=[self.product.id]))
        self.assertNotEqual(resp.status_code, 200)

    def test_shipping_station_instructions_are_shown(self):
        self.client.force_login(self.staff)
        body = self.client.get(reverse('instructions:product_doc', args=[self.product.id])).content.decode()
        self.assertIn('بند ارسال', body)

    def test_html_doc_has_kind_chips(self):
        self.client.force_login(self.staff)
        body = self.client.get(reverse('instructions:product_doc', args=[self.product.id])).content.decode()
        self.assertIn('data-kind-chip="step"', body)
        self.assertIn('data-kind-chip="note"', body)


class OrderDocAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _base_data(cls)

    def test_plain_logged_in_user_cannot_open_order_doc(self):
        plain = User.objects.create_user('shopper', password='x')
        self.client.force_login(plain)
        self.assertNotEqual(self.client.get(reverse('instructions:order_doc', args=[self.order.id])).status_code, 200)
