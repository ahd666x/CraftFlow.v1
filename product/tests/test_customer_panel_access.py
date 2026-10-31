"""r8: گیت دسترسی پنل نماینده (customer/*).

قبلاً ویوهای customer/* فقط @login_required داشتند؛ هر کاربر ثبت‌نام‌شدهٔ
فروشگاه می‌توانست وارد پنل شود، قیمت‌های عمده (base_price) را ببیند و سفارش
عمده ثبت کند — در تضاد با سیاست can_see_prices فروشگاه که قیمت فقط برای
نماینده/staff است. حالا representative_panel_required جلوی کاربر بی‌مجوز
می‌ایستد و او را به فروشگاه (/shop/) برمی‌گرداند.
"""
from django.test import TestCase
from django.contrib.auth.models import User, Group
from django.urls import reverse


class RepresentativePanelAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rep_group, _ = Group.objects.get_or_create(name='نماینده')
        cls.staff_group, _ = Group.objects.get_or_create(name='1')

        cls.representative = User.objects.create_user('rep', password='testpass')
        cls.representative.groups.add(cls.rep_group)

        cls.sales_staff = User.objects.create_user('sales', password='testpass')
        cls.sales_staff.groups.add(cls.staff_group)

        cls.staff_user = User.objects.create_user('staffie', password='testpass', is_staff=True)

        cls.superuser = User.objects.create_superuser('boss', password='testpass')

        cls.retail_user = User.objects.create_user('retail', password='testpass')

    def test_anonymous_redirects_to_login(self):
        response = self.client.get(reverse('customer_order_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_retail_user_redirected_to_shop(self):
        """کاربر عادیِ فروشگاه (بدون گروه) نباید قیمت عمده ببیند."""
        self.client.login(username='retail', password='testpass')
        response = self.client.get(reverse('customer_order_list'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/shop/')
        # پیام شفاف به کاربر
        follow = self.client.get(reverse('customer_order_list'), follow=True)
        messages_text = [str(m) for m in follow.context['messages']]
        self.assertTrue(any('نمایندگان' in m for m in messages_text))

    def test_representative_allowed(self):
        self.client.login(username='rep', password='testpass')
        response = self.client.get(reverse('customer_order_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'سفارش‌های من')

    def test_group1_sales_staff_allowed(self):
        self.client.login(username='sales', password='testpass')
        response = self.client.get(reverse('customer_order_list'))
        self.assertEqual(response.status_code, 200)

    def test_is_staff_user_allowed(self):
        self.client.login(username='staffie', password='testpass')
        response = self.client.get(reverse('customer_order_list'))
        self.assertEqual(response.status_code, 200)

    def test_superuser_allowed(self):
        self.client.login(username='boss', password='testpass')
        response = self.client.get(reverse('customer_order_list'))
        self.assertEqual(response.status_code, 200)

    def test_retail_user_blocked_from_shipments_and_create(self):
        self.client.login(username='retail', password='testpass')
        for url in [reverse('customer_shipments'), reverse('customer_create_order')]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertEqual(response.url, '/shop/', url)

    def test_retail_user_blocked_from_order_detail_of_others(self):
        self.client.login(username='retail', password='testpass')
        response = self.client.get(reverse('customer_order_detail', args=[999]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/shop/')

    def test_order_list_shows_jalali_date(self):
        """r8: تاریخ ثبت در لیست سفارش‌ها شمسی است (قبلاً str با خط تیره)."""
        from product.models import Order, Customer
        customer = Customer.objects.create(name='مشتری', phone='09120000000')
        order = Order.objects.create(user=self.representative, customer=customer, number='R8-1')
        # created_at از نوع PersianDateField است → jdatetime.date
        expected = order.created_at.strftime('%Y/%m/%d')
        self.client.login(username='rep', password='testpass')
        response = self.client.get(reverse('customer_order_list'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(expected, response.content.decode('utf-8'))
