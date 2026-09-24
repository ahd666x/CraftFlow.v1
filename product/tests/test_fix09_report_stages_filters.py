from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from decimal import Decimal

from product.models import (
    Product, ProductCategory, Order, OrderItem, PackagingUnit, ProductionLog,
    ProductionTask, Customer, STATION_CHOICES,
)


class ReportStagesFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('rsadmin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='RS1')
        # quantity=0 so no auto-created PackagingUnit rows
        cls.item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=0)

    def setUp(self):
        self.client.login(username='rsadmin', password='testpass')

    def _get(self, **params):
        return self.client.get(reverse('report_stages'), params)

    def test_none_when_no_units(self):
        response = self._get(packaging_status='none')
        self.assertEqual(response.status_code, 200)
        ids = [r['item'].id for r in response.context['report_data']]
        self.assertIn(self.item.id, ids)

    def test_not_in_pending_when_no_units(self):
        response = self._get(packaging_status='pending')
        self.assertEqual(response.status_code, 200)
        ids = [r['item'].id for r in response.context['report_data']]
        self.assertNotIn(self.item.id, ids)

    def test_pending_with_one_packed(self):
        from django.utils import timezone
        PackagingUnit.objects.create(order_item=self.item, unit_number=1, is_packed=True, is_shipped=False, packed_at=timezone.now())
        PackagingUnit.objects.create(order_item=self.item, unit_number=2, is_packed=False, is_shipped=False)
        response = self._get(packaging_status='pending')
        self.assertEqual(response.status_code, 200)
        ids = [r['item'].id for r in response.context['report_data']]
        self.assertIn(self.item.id, ids)

    def test_done_when_all_packed(self):
        from django.utils import timezone
        PackagingUnit.objects.create(order_item=self.item, unit_number=1, is_packed=True, is_shipped=False, packed_at=timezone.now())
        response = self._get(packaging_status='done')
        self.assertEqual(response.status_code, 200)
        ids = [r['item'].id for r in response.context['report_data']]
        self.assertIn(self.item.id, ids)

    def test_date_pack_filter_no_duplicate(self):
        from django.utils import timezone
        PackagingUnit.objects.create(order_item=self.item, unit_number=1, is_packed=True, is_shipped=False, packed_at=timezone.now())
        response = self._get(date_target='pack', date_from='1400/01/01', date_to='1410/01/01')
        self.assertEqual(response.status_code, 200)
        ids = [r['item'].id for r in response.context['report_data']]
        self.assertEqual(ids.count(self.item.id), 1)
        self.assertEqual(response.context['report_data'][0]['total_units'], 1)