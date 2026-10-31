from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse

from accounts.forms import ProfileUpdateForm, AddressForm
from storefront.models import Address


class PasswordChangePageTests(TestCase):
    """r7: قالب‌های تغییر رمز عبور وجود دارند و جریان کامل کار می‌کند.

    قبلاً 'accounts/password_change.html' وجود نداشت و کلیک روی
    «تغییر کلمه عبور» در پروفایل خطای 500 می‌داد.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="OldPass123x")
        self.client = Client()
        self.client.login(username="u1", password="OldPass123x")

    def test_password_change_get_returns_200(self):
        response = self.client.get(reverse('accounts:password_change'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'کلمه عبور فعلی')

    def test_wrong_old_password_re_renders_with_errors(self):
        response = self.client.post(reverse('accounts:password_change'), {
            'old_password': 'wrong-pass',
            'new_password1': 'NewPass456z',
            'new_password2': 'NewPass456z',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        # رمز عوض نشده باشد
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('OldPass123x'))

    def test_successful_change_redirects_to_done_page(self):
        response = self.client.post(reverse('accounts:password_change'), {
            'old_password': 'OldPass123x',
            'new_password1': 'NewPass456z',
            'new_password2': 'NewPass456z',
        })
        self.assertRedirects(response, reverse('accounts:password_change_done'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewPass456z'))

        done = self.client.get(reverse('accounts:password_change_done'))
        self.assertEqual(done.status_code, 200)
        self.assertContains(done, 'با موفقیت تغییر کرد')

    def test_password_change_requires_login(self):
        anon = Client()
        response = anon.get(reverse('accounts:password_change'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)


class AddressSoftDeleteTests(TestCase):
    """r7: حذف آدرس نرم است (is_active=False) نه فیزیکی — تا ردپای آدرس
    در سفارش‌های قبلی (Order.address) حفظ شود."""

    def setUp(self):
        self.user = User.objects.create_user(username="u2", password="pw")
        self.client = Client()
        self.client.login(username="u2", password="pw")
        self.address = Address.objects.create(
            user=self.user, title="خانه", recipient="علی",
            city="تهران", address="خیابان الف",
            is_default=True, is_active=True,
        )

    def test_delete_marks_inactive_and_clears_default(self):
        response = self.client.post(
            reverse('accounts:address_delete', args=[self.address.pk])
        )
        self.assertRedirects(response, reverse('accounts:address_list'))
        self.address.refresh_from_db()
        self.assertFalse(self.address.is_active)
        self.assertFalse(self.address.is_default)
        # رکورد فیزیکی هنوز وجود دارد
        self.assertTrue(Address.objects.filter(pk=self.address.pk).exists())

    def test_deleted_address_hidden_from_list(self):
        self.client.post(reverse('accounts:address_delete', args=[self.address.pk]))
        response = self.client.get(reverse('accounts:address_list'))
        self.assertEqual(response.status_code, 200)
        # تنها آدرس کاربر حذف شده → حالت خالی نمایش داده می‌شود
        self.assertContains(response, 'هنوز آدرسی ذخیره نشده است.')

    def test_cannot_delete_other_users_address(self):
        other = User.objects.create_user(username="u3", password="pw")
        addr_b = Address.objects.create(
            user=other, title="دفتر", recipient="رضا",
            city="شیراز", address="بلوار ب", is_active=True,
        )
        response = self.client.post(
            reverse('accounts:address_delete', args=[addr_b.pk]), follow=True
        )
        self.assertEqual(response.status_code, 404)
        addr_b.refresh_from_db()
        self.assertTrue(addr_b.is_active)


class ProfilePriceGuardTests(TestCase):
    """r7: مبلغ‌های پروفایل فقط برای کاربر با مجوز قیمت نمایش داده می‌شوند."""

    def setUp(self):
        self.client = Client()

    def test_plain_user_has_no_prices(self):
        User.objects.create_user(username="plain", password="pw")
        self.client.login(username="plain", password="pw")
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['show_prices'])
        self.assertNotIn('total_spent', response.context)
        self.assertNotContains(response, 'کل خریدها')

    def test_staff_user_sees_prices(self):
        User.objects.create_user(username="boss", password="pw", is_staff=True)
        self.client.login(username="boss", password="pw")
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_prices'])
        self.assertIn('total_spent', response.context)
        self.assertContains(response, 'کل خریدها')


class ProfileUpdateFormTests(TestCase):
    """r7: موبایل پروفایل مثل ثبت‌نام اعتبارسنجی می‌شود (ارقام فارسی هم OK)."""

    def test_persian_digits_converted(self):
        form = ProfileUpdateForm(data={'phone': '۰۹۱۲۳۴۵۶۷۸۹', 'bio': ''})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['phone'], '09123456789')

    def test_invalid_phone_rejected(self):
        form = ProfileUpdateForm(data={'phone': '12345', 'bio': ''})
        self.assertFalse(form.is_valid())
        self.assertIn('phone', form.errors)


class AddressFormWidgetTests(TestCase):
    """r7: فیلدهای فرم آدرس کلاس input دارند (قبلاً بدون استایل بودند)."""

    def test_fields_have_input_class(self):
        form = AddressForm()
        for name in ['title', 'recipient', 'province', 'city', 'postal_code', 'address']:
            self.assertIn('input', form.fields[name].widget.attrs.get('class', ''))
        checkbox = form.fields['is_default'].widget.attrs.get('class', '')
        self.assertIn('accent-primary-600', checkbox)


class PasswordResetPageTests(TestCase):
    """r8: بازبینی صفحات بازیابی رمز عبور.

    - confirm قبلاً گارد validlink نداشت؛ با لینک نامعتبر/منقضی فرم خالی
      + دکمهٔ بی‌اثر رندر می‌شد (بدون هیچ پیامی).
    - done عنوان و محتوای ناهمخوان داشت (عنوان: «تکمیل شد»، محتوا: «ایمیل
      ارسال شد»).
    - فرم درخواست، خطاهای فیلد ایمیل را نمایش نمی‌داد.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="resetee",
            password="OldPass123x",
            email="resetee@example.com",
        )
        self.client = Client()

    def _confirm_url(self, token):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        return reverse('accounts:password_reset_confirm', args=[uidb64, token])

    def test_request_page_renders_and_shows_email_errors(self):
        response = self.client.get(reverse('accounts:password_reset'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ارسال لینک بازیابی')

        # ایمیل نامعتبر → خطای فیلد باید در صفحه باشد
        bad = self.client.post(reverse('accounts:password_reset'), {'email': 'not-an-email'})
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, 'text-red-600')

    def test_done_page_says_email_sent(self):
        response = self.client.get(reverse('accounts:password_reset_done'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ایمیل بازیابی ارسال شد')
        # عنوان قدیمی ناهمخوان نباید برگردد
        self.assertNotContains(response, 'بازیابی رمز عبور تکمیل شد')

    def test_invalid_link_shows_warning_not_form(self):
        response = self.client.get(self._confirm_url('invalid-token'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'لینک بازیابی نامعتبر است')
        self.assertContains(response, 'درخواست لینک جدید')
        self.assertNotContains(response, 'name="new_password1"')

    def test_valid_link_sets_new_password(self):
        from django.contrib.auth.tokens import default_token_generator
        url = self._confirm_url(default_token_generator.make_token(self.user))

        # Django لینک معتبر را به توکن نشست «set-password» ریدایرکت می‌کند
        redirect = self.client.get(url)
        self.assertEqual(redirect.status_code, 302)
        set_url = redirect.url

        page = self.client.get(set_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'name="new_password1"')

        done = self.client.post(set_url, {
            'new_password1': 'BrandNew789k',
            'new_password2': 'BrandNew789k',
        })
        self.assertRedirects(done, reverse('accounts:password_reset_complete'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('BrandNew789k'))

    def test_mismatched_passwords_re_renders_with_errors(self):
        from django.contrib.auth.tokens import default_token_generator
        url = self._confirm_url(default_token_generator.make_token(self.user))
        set_url = self.client.get(url).url  # ریدایرکت به set-password
        response = self.client.post(set_url, {
            'new_password1': 'BrandNew789k',
            'new_password2': 'Different456j',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('OldPass123x'))

    def test_complete_page_renders(self):
        response = self.client.get(reverse('accounts:password_reset_complete'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'رمز عبور بازیابی شد')
