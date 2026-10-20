"""نمایش اندازه در صفحات سفارش: size_display باید همیشه مقدار معنادار بدهد."""
from decimal import Decimal

from django.test import TestCase

from product.models import Customer, Order, OrderItem, Product, ProductCategory


class SizeDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = ProductCategory.objects.create(name='میزتلویزیون')
        # محصول با ابعاد پیش‌فرض (سفارش استاندارد)
        cls.product = Product.objects.create(
            category=cls.category, name='فیدان', base_price=1000,
            length=200, width=60, height=75,
        )
        cls.simple_product = Product.objects.create(
            category=cls.category, name='بدون ابعاد', base_price=500,
        )
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(
            user=User_dummy(), customer=cls.customer, number='SZ-1',
        )

    def test_size_display_returns_stored_size_first(self):
        item = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=1,
            size='200x60x75', length=None, width=None, height=None,
        )
        self.assertEqual(item.size_display, '200x60x75')

    def test_size_display_falls_back_to_item_dimensions(self):
        """آیتمی که ابعاد سفارشی دارد ولی size خالی مانده (مسیرهای قدیمی)."""
        item = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=1,
            size='', length=180, width=55, height=None,
        )
        self.assertEqual(item.size_display, '180x55')

    def test_size_display_falls_back_to_product_default(self):
        """سفارش استاندارد: نه size و نه ابعاد سفارشی → اندازه‌ی پیش‌فرض محصول."""
        item = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=1,
            size='', length=None, width=None, height=None,
        )
        self.assertEqual(item.size_display, '200x60x75')

    def test_size_display_empty_when_nothing_known(self):
        item = OrderItem.objects.create(
            order=self.order, product=self.simple_product, quantity=1,
            size='', length=None, width=None, height=None,
        )
        self.assertEqual(item.size_display, '')

    def test_save_syncs_size_from_dimensions(self):
        """save() رشته‌ی size را از ابعاد ساختاریافته همگام می‌کند."""
        item = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=1,
            size='', length=180, width=55, height=None,
        )
        item.refresh_from_db()
        self.assertEqual(item.size, '180x55')

    def test_save_keeps_manual_size_without_dimensions(self):
        """مسیر ورود اکسل: size دستی بدون هیچ بعد سفارشی نباید پاک شود."""
        item = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=1,
            size='استاندارد بزرگ', length=None, width=None, height=None,
        )
        item.refresh_from_db()
        self.assertEqual(item.size, 'استاندارد بزرگ')


def User_dummy():
    from django.contrib.auth.models import User
    return User.objects.get_or_create(username='szadmin')[0]
