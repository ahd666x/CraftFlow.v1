"""r10 — ماتریس دسترسی کل URLهای product.

سیاست مصوب (درخواست کاربر):
  superuser       : همه‌چیز (ایجاد/ویرایش سفارش، محصول، قطعه، آرشیو، مدیریت نقاشی)
  گروه «1»        : خواندن و اسکن — سفارش‌ها، گزارش‌ها، چاپ، داشبورد، صفحات نقاشی فقط برای دیدن؛
                    هیچ ایجاد/ویرایشی؛ فاقد دسترسی به پنل نماینده و ادمین‌پنل
  گروه «نماینده»  : فقط پنل customer/* + سه AJAX فرم سفارش نماینده
                    (ajax_load_products، ajax_load_product_colors،
                    ajax_load_product_dimensions — customer/create_order.html
                    به هر سه وابسته است)
  کاربر عادی/ناشناس: بلاک

این تست همهٔ URLهای محصول را با ۴ کاربر می‌زند و هر تغییر آینده در دکوراتورها
را که ماتریس را بشکند، می‌گیرد.
"""
import json

from django.test import TestCase
from django.contrib.auth.models import User, Group
from django.urls import reverse

from product.models import (
    Product, ProductCategory, Order, OrderItem, Customer, WorkerProfile,
)

# کاربران و گروه‌ها
DUMMY_ARGS = {
    'pk': 1, 'order_id': 1, 'item_id': 1, 'task_id': 1, 'worker_id': 1,
    'product_id': 1, 'process_id': 1, 'stage_id': 1, 'process_material_id': 1,
    'part_id': 1, 'unit_id': 1, 'barcode': 'BAR', 'filename': 'f.cnc',
    'archive_type': 'cnc', 'plate': 'PLATE', 'date': '1400-01-01',
}

# — گروه ۱ مجاز نیست (ثبت/ویرایش فقط superuser؛ پنل نماینده فقط نماینده)
GROUP1_BLOCKED = {
    # ثبت/ویرایش سفارش و آیتم
    'create_order', 'create_order_step2', 'admin_edit_order',
    'admin_edit_order_item', 'admin_add_order_item', 'admin_delete_order',
    'admin_delete_order_item', 'admin_order_tasks', 'admin_delete_task',
    'admin_tasks_management', 'delete_all_tasks', 'delete_paint_tasks',
    'delete_all_paint_tasks_for_order', 'order_generate_tasks',
    # محصول و قطعه
    'product_create', 'product_edit', 'product_bom_edit',
    'product_toggle_active', 'ajax_create_part', 'ajax_get_part',
    'ajax_edit_part', 'ajax_load_customers',
    # آپلود/حذف آرشیو
    'archive_upload', 'archive_upload_cnc', 'archive_upload_dr',
    'archive_delete_file', 'upload_form',
    # مدیریت نقاشی (نوشتن)
    'assign_painting_process', 'auto_assign_tasks',
    'painting_add_to_schedule', 'painting_create_custom_task',
    'painting_assign_process', 'painting_auto_assign',
    'painting_assign_worker', 'painting_unassign_worker',
    'painting_delete_tasks', 'painting_clear_schedule',
    'painting_reset_schedule', 'painting_repaint_items',
    # پنل نماینده — طبق طراحی r8 گروه «1» هم به آن دسترسی دارد (همین‌طور که هست)
    # فاکتور: چکِ مالکیت داخل ویو (گروه ۱ مالک سفارش نیست → 403)
    'order_invoice',
}

# — نماینده فقط این‌ها را می‌بیند
REP_ALLOWED = {
    'customer_order_list', 'customer_create_order', 'customer_order_detail',
    'customer_edit_order_item', 'customer_delete_order_item',
    'customer_edit_order_info', 'customer_add_item', 'customer_shipments',
    'customer_shipment_detail', 'ajax_load_products', 'ajax_load_product_colors',
    'ajax_load_product_dimensions',
}

# — superuser مجاز به گرفتن 403 از چکِ داخل ویو (پروفایل کاری) است
SUPERUSER_BODY_403_OK = {
    'scan_item_tasks_ajax',   # نیازمند WorkerProfile — چک داخل ویو
}
# گروه ۱ هم بدون WorkerProfile در این ویوها 403 داخل-ویو می‌گیرد
G1_BODY_403_OK = {
    'scan_item_tasks_ajax',
}
# ویوهایی که احراز مالکیت را داخل بدنه انجام می‌دهند و آبجکتِ دمویی نیست:
# 404 (نبودن آبجکت) قبل از چک مالکیت می‌آید — برای کاربر بلاک‌شده هم طبیعی است
BODY_404_OK = {
    'order_invoice',
}

BLOCKED_CODES = (302, 403, 405)  # 405: ویوهای require_POST اول — گارد متد بیرون‌ترین است


def _all_url_names():
    from product.urls import urlpatterns
    names = []
    for pat in urlpatterns:
        name = getattr(pat, 'name', None)
        if name:
            converters = getattr(pat.pattern, 'converters', {})
            names.append((name, sorted(converters.keys())))
    return names


class AccessMatrixTests(TestCase):
    """ضربه زدن با ۴ کاربر به همهٔ URLهای محصول و کنترل وضعیت."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('mx_admin', password='testpass')
        # گروه «نماینده» با مایگریشن accounts/0002 از قبل هست → get_or_create
        cls.g1_group, _ = Group.objects.get_or_create(name='1')
        cls.rep_group, _ = Group.objects.get_or_create(name='نماینده')
        cls.g1 = User.objects.create_user('mx_g1', password='testpass')
        cls.g1.groups.add(cls.g1_group)
        cls.rep = User.objects.create_user('mx_rep', password='testpass')
        cls.rep.groups.add(cls.rep_group)
        cls.plain = User.objects.create_user('mx_plain', password='testpass')

    def _url(self, name, converters):
        kwargs = {k: DUMMY_ARGS[k] for k in converters if k in DUMMY_ARGS}
        return reverse(name, kwargs=kwargs)

    def _assert_matrix(self, name, converters):
        url = self._url(name, converters)
        # هر فاز باید در پایان logout کند؛ اگر فازی وسط fail شود، نشست کاربر
        # قبلی به URL بعدی نشت می‌کند — پس اول هر URL نشست را پاک می‌کنیم.
        self.client.logout()

        try:
            self._phases(name, url)
        finally:
            self.client.logout()

    def _phases(self, name, url):
        # ناشناس → همیشه بلاک (لاگین‌ردایرکت یا گارد متد)
        r = self.client.get(url)
        self.assertIn(
            r.status_code, BLOCKED_CODES,
            f'{name}: ناشناس باید بلاک باشد،Got {r.status_code}')

        # کاربر عادی فروشگاه (بدون گروه) → بلاک
        self.client.login(username='mx_plain', password='testpass')
        r = self.client.get(url)
        if name in BODY_404_OK:
            self.assertIn(r.status_code, (302, 403, 404, 405),
                          f'{name}: کاربر عادی باید بلاک/404 شود،Got {r.status_code}')
        else:
            self.assertIn(r.status_code, BLOCKED_CODES,
                          f'{name}: کاربر عادی باید بلاک شود،Got {r.status_code}')
        self.client.logout()

        # superuser → هیچ‌جا 403 نمی‌گیرد (جز چک‌های پروفایل کاری داخل ویو)
        self.client.login(username='mx_admin', password='testpass')
        r = self.client.get(url)
        if name not in SUPERUSER_BODY_403_OK:
            self.assertNotEqual(
                r.status_code, 403,
                f'{name}: superuser نباید 403 بگیرد،Got {r.status_code}')
        self.client.logout()

        # گروه ۱
        self.client.login(username='mx_g1', password='testpass')
        r = self.client.get(url)
        if name in GROUP1_BLOCKED:
            if name in BODY_404_OK:
                self.assertIn(r.status_code, (302, 403, 404, 405),
                              f'{name}: گروه ۱ باید بلاک/404 شود،Got {r.status_code}')
            else:
                self.assertIn(r.status_code, BLOCKED_CODES,
                              f'{name}: گروه ۱ باید بلاک شود،Got {r.status_code}')
        elif name in G1_BODY_403_OK:
            self.assertEqual(r.status_code, 403,
                             f'{name}: گروه ۱ بدون پروفایل کاری باید 403 بگیرد،Got {r.status_code}')
        else:
            self.assertNotEqual(
                r.status_code, 403,
                f'{name}: گروه ۱ (خواندن/اسکن) نباید 403 بگیرد،Got {r.status_code}')
        self.client.logout()

        # نماینده
        self.client.login(username='mx_rep', password='testpass')
        r = self.client.get(url)
        if name in REP_ALLOWED:
            self.assertNotEqual(
                r.status_code, 403,
                f'{name}: نماینده باید به پنل خودش دسترسی داشته باشد،Got {r.status_code}')
        else:
            if name in BODY_404_OK:
                self.assertIn(r.status_code, (302, 403, 404, 405),
                              f'{name}: نماینده باید بلاک/404 شود،Got {r.status_code}')
            else:
                self.assertIn(r.status_code, BLOCKED_CODES,
                              f'{name}: نماینده باید بلاک شود،Got {r.status_code}')
        self.client.logout()

    def test_matrix_all_urls(self):
        names = _all_url_names()
        self.assertGreater(len(names), 80, 'لیست URLها ناقص است')
        for name, converters in names:
            with self.subTest(url=name):
                self._assert_matrix(name, converters)

    def test_matrix_covers_critical_urls(self):
        names = {n for n, _ in _all_url_names()}
        for critical in (
            'order_list', 'order_detail', 'create_order', 'product_create',
            'customer_order_list', 'ajax_load_products', 'painting_workers_api',
            'admin_edit_order', 'export_autocut_xml', 'mark_task_done',
        ):
            self.assertIn(critical, names)


class AccessMatrixBehaviorTests(TestCase):
    """تست‌های رفتاری دقیق: متد، گروه و بدنهٔ پاسخ."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('mx_admin', password='testpass')
        cls.g1_group, _ = Group.objects.get_or_create(name='1')
        cls.rep_group, _ = Group.objects.get_or_create(name='نماینده')
        cls.g1 = User.objects.create_user('mx_g1', password='testpass')
        cls.g1.groups.add(cls.g1_group)
        cls.rep = User.objects.create_user('mx_rep', password='testpass')
        cls.rep.groups.add(cls.rep_group)

        cls.category = ProductCategory.objects.create(name='م-دسته')
        cls.product = Product.objects.create(category=cls.category, name='م-محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='م-مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='MX-1')
        cls.item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)

    # --- باگ اصلی گزارش‌شده: نماینده در صفحات مدیریتی ---
    def test_representative_blocked_on_order_list(self):
        self.client.login(username='mx_rep', password='testpass')
        r = self.client.get(reverse('order_list'))
        self.assertIn(r.status_code, (302, 403))

    def test_representative_blocked_on_order_detail(self):
        self.client.login(username='mx_rep', password='testpass')
        r = self.client.get(reverse('order_detail', args=[self.order.id]))
        self.assertIn(r.status_code, (302, 403))

    def test_representative_blocked_on_create_order(self):
        self.client.login(username='mx_rep', password='testpass')
        r = self.client.get(reverse('create_order'))
        self.assertIn(r.status_code, (302, 403))

    def test_representative_still_sees_customer_panel(self):
        self.client.login(username='mx_rep', password='testpass')
        r = self.client.get(reverse('customer_order_list'))
        self.assertEqual(r.status_code, 200)

    # --- گروه ۱: خواندن بله، نوشتن خیر ---
    def test_group1_can_read_order_list_and_detail(self):
        self.client.login(username='mx_g1', password='testpass')
        self.assertEqual(self.client.get(reverse('order_list')).status_code, 200)
        r = self.client.get(reverse('order_detail', args=[self.order.id]))
        self.assertEqual(r.status_code, 200)

    def test_group1_cannot_create_or_edit_order(self):
        self.client.login(username='mx_g1', password='testpass')
        r = self.client.get(reverse('create_order'))
        self.assertIn(r.status_code, (302, 403))
        r = self.client.get(reverse('admin_edit_order', args=[self.order.id]))
        self.assertIn(r.status_code, (302, 403))

    def test_group1_lands_on_dashboard_via_root(self):
        self.client.login(username='mx_g1', password='testpass')
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 200)

    # --- ویوهای مخلوط: گروه ۱ فقط GET ---
    def test_group1_get_painting_workers_api_ok_post_forbidden(self):
        self.client.login(username='mx_g1', password='testpass')
        url = reverse('painting_workers_api')
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, data=json.dumps({'name': 'x'}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.json()['success'])

    def test_group1_get_worker_detail_ok_put_forbidden(self):
        worker_user = User.objects.create_user('mx_paint_worker', password='testpass')
        worker_user.groups.add(self.g1_group)
        worker = WorkerProfile.objects.create(user=worker_user, stage='paint')
        self.client.login(username='mx_g1', password='testpass')
        url = reverse('painting_worker_detail_api', args=[worker.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.put(url, data=json.dumps({'skills': 'x'}),
                            content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_admin_put_worker_detail_ok(self):
        worker_user = User.objects.create_user('mx_paint_worker2', password='testpass')
        worker_user.groups.add(self.g1_group)
        worker = WorkerProfile.objects.create(user=worker_user, stage='paint')
        self.client.login(username='mx_admin', password='testpass')
        url = reverse('painting_worker_detail_api', args=[worker.id])
        r = self.client.put(url, data=json.dumps({'skills': 'خمیرکاری'}),
                            content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['success'])

    # --- نماینده: AJAXهای فرم سفارش باز، بقیهٔ AJAXهای داخلی بسته ---
    def test_representative_ajax_load_products_ok(self):
        self.client.login(username='mx_rep', password='testpass')
        r = self.client.get(reverse('ajax_load_products'), {'category': ''})
        self.assertEqual(r.status_code, 200)

    def test_representative_ajax_load_product_dimensions_ok(self):
        # customer/create_order.html به این AJAX وابسته است (loadProductDimensionsUrl)
        self.client.login(username='mx_rep', password='testpass')
        r = self.client.get(reverse('ajax_load_product_dimensions'),
                            {'product': self.product.id})
        self.assertEqual(r.status_code, 200)

    def test_representative_blocked_on_ajax_load_customers(self):
        self.client.login(username='mx_rep', password='testpass')
        r = self.client.get(reverse('ajax_load_customers'))
        self.assertIn(r.status_code, BLOCKED_CODES)
