from django.test import TestCase
from django.forms import modelform_factory
from django import forms

import jdatetime

from product.fields import PersianDateField, PersianDateFormField
from product.models import Order, OrderItem, Product, ProductCategory, Customer
from django.contrib.auth.models import User


class PersianDateFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('pdadmin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='PD1')

    def test_form_renders_shamsi_value(self):
        from product.forms import OrderEditForm
        instance = Order.objects.get(pk=self.order.id)
        instance.due_date = jdatetime.date(1405, 6, 31)
        instance.save()
        form = OrderEditForm(instance=instance)
        rendered = form.as_p()
        self.assertIn('1405-06-31', rendered)

    def test_save_preserves_shamsi_value(self):
        from product.forms import OrderEditForm
        instance = Order.objects.get(pk=self.order.id)
        instance.due_date = jdatetime.date(1405, 6, 31)
        instance.save()
        form = OrderEditForm({
            'customer': self.customer.id, 'number': 'PD1', 'due_date': '1405-06-31',
            'priority': 3, 'status': 'draft',
        }, instance=instance)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        from django.db import connection
        expected = jdatetime.date(1405, 6, 31).togregorian()
        with connection.cursor() as cursor:
            cursor.execute('SELECT due_date FROM product_order WHERE id = %s', [saved.id])
            raw = cursor.fetchone()[0]
        raw_str = raw.isoformat() if hasattr(raw, 'isoformat') else str(raw)
        self.assertEqual(raw_str, expected.isoformat())

    def test_accepts_persian_digits_and_slashes(self):
        field = PersianDateFormField()
        self.assertEqual(field.to_python('1405/06/31'), jdatetime.date(1405, 6, 31))
        self.assertEqual(field.to_python('۱۴۰۵-۰۶-۳۱'), jdatetime.date(1405, 6, 31))

    def test_rejects_invalid(self):
        field = PersianDateFormField()
        for bad in ('abc', '1405-13-40'):
            try:
                field.to_python(bad)
                self.fail(f'{bad} should have raised')
            except forms.ValidationError:
                pass

    def test_empty_returns_none(self):
        field = PersianDateFormField()
        self.assertIsNone(field.to_python(''))

    def test_modelform_factory_uses_text_input(self):
        FormCls = modelform_factory(Order, fields=['created_at', 'due_date'])
        form = FormCls()
        for name in ('created_at', 'due_date'):
            widget = form.fields[name].widget
            self.assertIsInstance(widget, forms.TextInput, name)


class RepairPersianDatesCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('rdadmin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='RD1')

    def test_dry_run_no_change(self):
        from django.core.management import call_command
        from io import StringIO
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("UPDATE product_order SET created_at = '1405-06-31' WHERE id = %s", [self.order.id])
        out = StringIO()
        call_command('repair_persian_dates', stdout=out)
        # In dry-run mode nothing should be changed in the DB
        with connection.cursor() as cursor:
            cursor.execute('SELECT CAST(created_at AS TEXT) FROM product_order WHERE id = %s', [self.order.id])
            raw = cursor.fetchone()[0]
        self.assertEqual(raw, '1405-06-31')
        self.assertIn('هیچ تغییری', out.getvalue())

    def test_apply_fixes_value(self):
        from django.core.management import call_command
        from io import StringIO
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("UPDATE product_order SET created_at = '1405-06-31' WHERE id = %s", [self.order.id])
        out = StringIO()
        call_command('repair_persian_dates', '--apply', stdout=out)
        with connection.cursor() as cursor:
            cursor.execute('SELECT CAST(created_at AS TEXT) FROM product_order WHERE id = %s', [self.order.id])
            raw = cursor.fetchone()[0]
        self.assertEqual(raw, jdatetime.date(1405, 6, 31).togregorian().isoformat())