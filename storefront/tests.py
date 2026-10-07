"""
تست‌های رگرسیون لایه‌ی فروشگاه.

این فایل عمداً روی رفتاری تمرکز دارد که پورت کردنش می‌توانست تولید را
بی‌سروصدا خراب کند:

* قیمت‌گذاری سه‌محوره و فلگ‌های `editable`
* قفل وضعیت سفارش (تسک نباید `paid` را بازنویسی کند)
* اسلاگ یونیکد فارسی
"""
from decimal import Decimal
from django.test import TestCase

from product.models import (
    Customer, Order, OrderItem, Part, Product, ProductBOM, ProductCategory,
)
from storefront.pricing import calculate_dimension_price


class DimensionPricingTests(TestCase):
    """قیمت باید از هر سه محور مستقل حساب شود، نه فقط از طول."""

    def test_no_dimensions_returns_base_price(self):
        self.assertEqual(calculate_dimension_price(
            base_price=1000,
            default_length=100, default_width=60, default_height=40,
            length_percent=2, width_percent=2, height_percent=2,
            length_editable=True, width_editable=True, height_editable=True,
            length=None, width=None, height=None,
        ), 1000)

    def test_length_diff_adds_increase(self):
        # ۱۰ سانتی‌متر اختلاف × ۲٪ × ۱۰۰۰ = ۲۰۰
        self.assertEqual(calculate_dimension_price(
            base_price=1000,
            default_length=100, default_width=60, default_height=40,
            length_percent=2, width_percent=2, height_percent=2,
            length_editable=True, width_editable=True, height_editable=True,
            length=110, width=60, height=40,
        ), 1200)

    def test_width_and_height_are_independent_axes(self):
        # طول بدون تغییر، عرض ۱۰ و ارتفاع ۵ سانتی‌متر بیشتر
        # ۱۰×۲ + ۵×۲ = ۳۰٪ → ۳۰۰
        self.assertEqual(calculate_dimension_price(
            base_price=1000,
            default_length=100, default_width=60, default_height=40,
            length_percent=2, width_percent=2, height_percent=2,
            length_editable=True, width_editable=True, height_editable=True,
            length=100, width=70, height=45,
        ), 1300)

    def test_non_editable_axis_is_ignored_entirely(self):
        # عرض غیرقابل تغییر است، پس حتی با مقدار متفاوت هم اثری ندارد.
        self.assertEqual(calculate_dimension_price(
            base_price=1000,
            default_length=100, default_width=60, default_height=40,
            length_percent=2, width_percent=50, height_percent=2,
            length_editable=True, width_editable=False, height_editable=True,
            length=110, width=200, height=40,
        ), 1200)

    def test_shrinking_dimensions_never_goes_negative(self):
        self.assertEqual(calculate_dimension_price(
            base_price=1000,
            default_length=100, default_width=60, default_height=40,
            length_percent=2, width_percent=2, height_percent=2,
            length_editable=True, width_editable=True, height_editable=True,
            length=1, width=1, height=1,
        ), 0)

    def test_missing_default_dimension_yields_zero_diff(self):
        # اگر بعدِ پیش‌فرض محصول تعریف نشده باشد، آن بعد قیمت را جابه‌جا نمی‌کند.
        self.assertEqual(calculate_dimension_price(
            base_price=1000,
            default_length=None, default_width=60, default_height=None,
            length_percent=10, width_percent=2, height_percent=10,
            length_editable=True, width_editable=True, height_editable=True,
            length=500, width=60, height=900,
        ), 1000)


class ProductSlugTests(TestCase):
    def setUp(self):
        self.category = ProductCategory.objects.create(name="دسته تست")

    def test_persian_slug_is_generated(self):
        # `slugify(allow_unicode=True)` فاصله را به خط تیره تبدیل می‌کند ولی
        # حروف فارسی را نگه می‌دارد — که همان چیزی است که برای URL فارسی می‌خواهیم.
        p = Product.objects.create(category=self.category, name="مبل راحتی", base_price=100)
        self.assertEqual(p.slug, "مبل-راحتی")
        self.assertIn("مبل", p.slug)

    def test_duplicate_names_get_numeric_suffix(self):
        a = Product.objects.create(category=self.category, name="دایان", base_price=100)
        b = Product.objects.create(category=self.category, name="دایان", base_price=200)
        self.assertNotEqual(a.slug, b.slug)
        self.assertTrue(b.slug.startswith("دایان"))

    def test_explicit_slug_is_preserved(self):
        p = Product.objects.create(
            category=self.category, name="تست", slug="slug-dahan", base_price=100,
        )
        self.assertEqual(p.slug, "slug-dahan")

    def test_category_gets_slug_too(self):
        c = ProductCategory.objects.create(name="مبل راحتی")
        self.assertEqual(c.slug, "مبل-راحتی")

    def test_active_objects_hides_inactive(self):
        Product.objects.create(category=self.category, name="فعال", base_price=1)
        Product.objects.create(category=self.category, name="غیرفعال", base_price=1, is_active=False)
        names = set(Product.active_objects.values_list('name', flat=True))
        self.assertIn('فعال', names)
        self.assertNotIn('غیرفعال', names)


class OrderLockedStatusTests(TestCase):
    """
    بازمحاسبه‌ی وضعیت سفارش از روی تسک‌ها نباید وضعیت‌های مالی/ارسال را
    بازنویسی کند. بدون این قفل، هر بار که کارگری تسکی را done می‌کند،
    سفارشِ `paid` به `producing` برمی‌گردد.
    """

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user(username="tester", password="pw")
        self.customer = Customer.objects.create(name="مشتری تست")
        self.category = ProductCategory.objects.create(name="دسته")
        self.product = Product.objects.create(category=self.category, name="محصول", base_price=500)
        self.order = Order.objects.create(user=self.user, customer=self.customer, status='paid')
        self.item = OrderItem.objects.create(order=self.order, product=self.product, quantity=1)

    def test_paid_status_survives_task_completion(self):
        # مسیر واقعی: `ProductionTask.save()` خودش `update_order_status()` را صدا می‌زند.
        task = self._make_task()
        task.status = 'done'
        task.save()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'paid')

    def test_all_commerce_statuses_are_locked(self):
        for status in ('paid', 'shipped', 'delivered', 'cancelled'):
            self.order.status = status
            self.order.save(update_fields=['status'])
            task = self._make_task()
            task.status = 'done'
            task.save()
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, status)

    def _make_task(self):
        from product.models import Material, Part, ProductionTask
        material = Material.objects.create(name=f"ورق {ProductionTask.objects.count()}", thickness=Decimal("16.0"))
        part = Part.objects.create(
            material=material, name="قطعه", length=Decimal("10"), width=Decimal("5"),
            pname="محصول", routing_code="cnc",
        )
        return ProductionTask.objects.create(
            order=self.order, part=part, station_name='cut',
            step_order=1, quantity=1, status='pending',
        )

    def test_production_statuses_still_advance(self):
        """قفل نباید چرخه‌ی تولید را متوقف کند."""
        # سفارش باید در وضعیتی باشد که قفل رویش فعال نیست.
        self.order.status = 'planned'
        self.order.save(update_fields=['status'])
        task = self._make_task()
        task.status = 'done'
        task.save()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'completed')

    def test_locked_statuses_constant_is_declared(self):
        self.assertEqual(
            Order.LOCKED_STATUSES, {'paid', 'shipped', 'delivered', 'cancelled'}
        )


class OrderMoneyFieldTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user(username="buyer", password="pw")
        self.customer = Customer.objects.create(name="خریدار")
        self.category = ProductCategory.objects.create(name="دسته")
        self.product = Product.objects.create(category=self.category, name="کالا", base_price=1000)

    def test_new_order_starts_with_zero_amounts(self):
        order = Order.objects.create(user=self.user, customer=self.customer)
        self.assertEqual(order.total_amount, 0)
        self.assertEqual(order.discount_amount, 0)
        self.assertEqual(order.final_amount, 0)

    def test_order_item_holds_custom_dimensions(self):
        order = Order.objects.create(user=self.user, customer=self.customer)
        item = OrderItem.objects.create(
            order=order, product=self.product, quantity=2, length=120, width=70, height=45,
        )
        item.refresh_from_db()
        self.assertEqual((item.length, item.width, item.height), (120, 70, 45))
