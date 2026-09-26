"""تست‌های E2: ثبت هم‌زمان چند بخش رنگی خراب در فرم اسکن بسته‌بندی مونتاژ."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from product.models import (
    Color,
    Customer,
    Order,
    OrderItem,
    PackagingUnit,
    PaintingProcess,
    Product,
    ProductCategory,
    ProductionDefect,
    ProductionTask,
    WorkerProfile,
)


class MultiPartDefectReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('e2user', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته E2')
        cls.product = Product.objects.create(category=cls.category, name='محصول E2', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری E2', phone='09120000000')
        cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='E2')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=1)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        Color.objects.create(part='درب', code='9', orderitem=cls.order_item)
        cls.unit = PackagingUnit.objects.get(order_item=cls.order_item, unit_number=1)
        cls.process = PaintingProcess.objects.create(
            name='روند E2', code='E2', color_codes=['8', '9'], is_active=True,
        )
        for step, part in enumerate(['بدنه', 'درب'], start=1):
            ProductionTask.objects.create(
                order=cls.order, order_item=cls.order_item, station_name='paint',
                color_part=part, step_order=step, quantity=1, status='pending',
            )

    def setUp(self):
        self.client.login(username='e2user', password='testpass')
        WorkerProfile.objects.create(user=self.user, stage='mon')

    def _post(self, parts, description='خرابی هم‌زمان'):
        return self.client.post(
            reverse('scan_packaging_unit', args=[self.unit.id]),
            {'color_parts': parts, 'description': description},
        )

    def test_two_parts_create_two_defects_in_one_submission(self):
        response = self._post(['بدنه', 'درب'])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProductionDefect.objects.count(), 2)
        by_part = {
            defect.color_part: defect
            for defect in ProductionDefect.objects.filter(packaging_unit=self.unit)
        }
        self.assertEqual(set(by_part), {'بدنه', 'درب'})
        for part, defect in by_part.items():
            self.assertEqual(defect.order_item, self.order_item)
            self.assertEqual(defect.quantity, 1)
            self.assertEqual(defect.description, 'خرابی هم‌زمان')
            self.assertEqual(defect.reported_by, self.user)
            self.assertIsNotNone(defect.task)
            self.assertEqual(defect.task.color_part, part)

    def test_existing_defect_part_is_skipped_with_warning(self):
        ProductionDefect.objects.create(
            task=None, order=self.order, order_item=self.order_item,
            packaging_unit=self.unit, color_part='بدنه', quantity=1,
            description='خرابی قبلی', reported_by=self.user, status='reported',
        )

        response = self._post(['بدنه', 'درب'])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProductionDefect.objects.count(), 2)
        new_defect = ProductionDefect.objects.get(packaging_unit=self.unit, color_part='درب')
        self.assertEqual(new_defect.description, 'خرابی هم‌زمان')
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('بدنه' in m and 'قبلاً خرابی ثبت شده بود' in m for m in messages))
        self.assertTrue(any('درب' in m and 'ثبت شد' in m for m in messages))

    def test_no_checkbox_selected_shows_error_and_creates_nothing(self):
        response = self._post([])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProductionDefect.objects.count(), 0)
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('حداقل یک بخش خراب را انتخاب کنید.' in m for m in messages))

    def test_get_renders_checkbox_per_available_part(self):
        response = self.client.get(reverse('scan_packaging_unit', args=[self.unit.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="color_parts"')
        self.assertContains(response, 'value="بدنه"')
        self.assertContains(response, 'value="درب"')
        self.assertContains(response, 'type="checkbox"')
        self.assertNotContains(response, '<select name="color_parts"')
        self.assertNotContains(response, 'name="color_part"')

    def test_part_not_in_available_parts_is_ignored(self):
        response = self._post(['بدنه', 'رینگ'])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProductionDefect.objects.count(), 1)
        self.assertTrue(ProductionDefect.objects.filter(color_part='بدنه').exists())
