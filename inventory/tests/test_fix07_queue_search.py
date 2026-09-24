from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from decimal import Decimal

from inventory.models import (
    RawMaterial, RawMaterialCategory, StockMovement, MaterialIssue,
)
from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    ProductionDefect, PackagingUnit, PaintingProcess, Customer, Color,
)


class QueueSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('queueuser', password='testpass')
        cls.cat = RawMaterialCategory.objects.create(name=' مواد')
        cls.raw = RawMaterial.objects.create(
            category=cls.cat, name='ماده تست', code='Q001', unit='kg',
        )
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(
            category=cls.category, name='محصول جستجو', base_price=1000,
        )
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='SEARCH1')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)

        cls.process = PaintingProcess.objects.create(
            name='روند جستجو', code='SEARCH', color_codes=['8'], is_active=True,
        )

        cls.task = ProductionTask.objects.create(
            order=cls.order, order_item=cls.order_item, station_name='mon',
            quantity=2, status='pending', step_order=1,
        )
        # issue linked to task (non-paint)
        cls.issue_task = MaterialIssue.objects.create(
            task=cls.task, raw_material=cls.raw, requested_quantity=Decimal('1'),
            purpose='production', status='requested', requested_by=cls.user,
        )
        # issue linked to defect
        cls.defect = ProductionDefect.objects.create(
            order=cls.order, order_item=cls.order_item, color_part='بدنه',
            quantity=1, description='خرابی جستجو', reported_by=cls.user,
        )
        cls.issue_defect = MaterialIssue.objects.create(
            defect=cls.defect, raw_material=cls.raw, requested_quantity=Decimal('1'),
            purpose='rework', status='requested', requested_by=cls.user,
        )
        # issue linked to order_item (painting style)
        cls.issue_item = MaterialIssue.objects.create(
            order_item=cls.order_item, raw_material=cls.raw, requested_quantity=Decimal('1'),
            purpose='production', status='requested', requested_by=cls.user,
            color_part='بدنه', painting_process=cls.process,
        )

    def setUp(self):
        self.client.login(username='queueuser', password='testpass')

    def test_search_by_order_id_finds_all_three(self):
        response = self.client.get(reverse('inventory:production_issue_queue'), {'q': str(self.order.id)})
        self.assertEqual(response.status_code, 200)
        ids = [i.id for i in response.context['issues']]
        self.assertIn(self.issue_task.id, ids)
        self.assertIn(self.issue_defect.id, ids)
        self.assertIn(self.issue_item.id, ids)

    def test_search_by_product_name(self):
        response = self.client.get(
            reverse('inventory:production_issue_queue'), {'q': 'محصول'}
        )
        self.assertEqual(response.status_code, 200)
        ids = [i.id for i in response.context['issues']]
        self.assertIn(self.issue_task.id, ids)
        self.assertIn(self.issue_defect.id, ids)
        self.assertIn(self.issue_item.id, ids)

    def test_search_no_match(self):
        response = self.client.get(reverse('inventory:production_issue_queue'), {'q': 'zzz'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['issues']), [])