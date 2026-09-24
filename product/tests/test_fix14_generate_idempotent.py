from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from decimal import Decimal

from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    Customer, Color, PaintingProcess, PaintingStage, Material, Part, ProductBOM,
)
from inventory.models import RawMaterial, RawMaterialCategory


class GenerateIdempotentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('gidadmin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='GID1')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        cls.process = PaintingProcess.objects.create(name='روند', code='GID', color_codes=['8'], is_active=True)
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='دهان', duration_minutes=30,
            drying_time_minutes=60, required_skill='painter',
        )
        cls.raw_cat = RawMaterialCategory.objects.create(name='ماده')
        cls.raw = RawMaterial.objects.create(category=cls.raw_cat, name='ورق', code='RM1', unit='kg')
        cls.material = Material.objects.create(name='ماده', thickness=Decimal('1'), raw_material=cls.raw, consumption_per_unit=Decimal('1'))
        cls.part = Part.objects.create(
            material=cls.material, name='قطعه', length=Decimal('1'), width=Decimal('1'),
            pname='محصول', routing_code='cut',
        )
        ProductBOM.objects.create(product=cls.product, part=cls.part, quantity=1)

    def setUp(self):
        self.client.login(username='gidadmin', password='testpass')

    def test_second_generate_is_blocked(self):
        self.order.generate_tasks()
        count_before = ProductionTask.objects.filter(order=self.order).count()
        result = self.order.generate_tasks()
        self.assertFalse(result['success'])
        self.assertEqual(ProductionTask.objects.filter(order=self.order).count(), count_before)

    def test_post_second_generate_returns_false(self):
        self.order.generate_tasks()
        response = self.client.post(reverse('order_generate_tasks', args=[self.order.id]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])

    def test_after_delete_all_tasks_generate_allowed(self):
        self.order.generate_tasks()
        self.client.post(reverse('delete_all_tasks', args=[self.order.id]))
        result = self.order.generate_tasks()
        self.assertTrue(result['success'])

    def test_order_with_only_paint_task_can_generate_non_paint(self):
        item2 = OrderItem.objects.create(order=self.order, product=self.product, quantity=1)
        Color.objects.create(part='بدنه', code='8', orderitem=item2)
        self.order.generate_tasks()
        # Delete ALL tasks for item2; item1 still has non-paint tasks → blocked
        ProductionTask.objects.filter(order_item=item2).delete()
        result = self.order.generate_tasks()
        self.assertFalse(result['success'])