from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.core.management import call_command
from io import StringIO
from decimal import Decimal

from inventory.models import (
    RawMaterial, RawMaterialCategory, StockMovement, MaterialIssue, MaterialLeftover,
)
from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask, ProductionDefect,
    PackagingUnit, Customer, Color, PaintingProcess,
)


class IssueMaterialTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('imuser', password='testpass')
        cls.cat = RawMaterialCategory.objects.create(name=' مواد')
        cls.raw = RawMaterial.objects.create(
            category=cls.cat, name='ماده', code='IM01', unit='kg', pack_size=4,
        )
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='IM1')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        cls.process = PaintingProcess.objects.create(name='روند', code='IM', color_codes=['8'], is_active=True)

    def setUp(self):
        self.client.login(username='imuser', password='testpass')
        StockMovement.objects.create(raw_material=self.raw, movement_type='purchase', quantity=Decimal('100'), note='م-existing')

    def _defect_only_issue(self):
        defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.order_item, color_part='بدنه',
            quantity=1, description='خرابی', reported_by=self.user,
        )
        return MaterialIssue.objects.create(
            defect=defect, raw_material=self.raw, requested_quantity=Decimal('3'),
            purpose='rework', status='requested', requested_by=self.user,
        )

    def test_issue_only_with_defect(self):
        issue = self._defect_only_issue()
        response = self.client.post(reverse('inventory:issue_material', args=[issue.id]), {'quantity': '3'})
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'issued')
        self.assertIsNotNone(issue.stock_movement)
        self.assertEqual(issue.stock_movement.reference_order_item, self.order_item)

    def test_invalid_quantity(self):
        issue = self._defect_only_issue()
        response = self.client.post(reverse('inventory:issue_material', args=[issue.id]), {'quantity': 'abc'})
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'requested')
        self.assertIsNone(issue.stock_movement)

    def test_pack_size_leftover(self):
        issue = self._defect_only_issue()
        response = self.client.post(reverse('inventory:issue_material', args=[issue.id]), {'quantity': '3'})
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        leftover = MaterialLeftover.objects.get(raw_material=self.raw)
        self.assertEqual(leftover.quantity, Decimal('1'))
        movement = issue.stock_movement
        self.assertEqual(movement.quantity, Decimal('3'))

    def test_insufficient_stock(self):
        issue = self._defect_only_issue()
        StockMovement.objects.filter(raw_material=self.raw).update(quantity=Decimal('2'))
        response = self.client.post(reverse('inventory:issue_material', args=[issue.id]), {'quantity': '3'})
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'requested')
        self.assertIsNone(issue.stock_movement)