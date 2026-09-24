from django.test import TestCase
from django.contrib.auth.models import User
from django.core.management import call_command
from io import StringIO
from decimal import Decimal

from product.utils import auto_create_material_issues
from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    Customer, Color, PaintingProcess, PaintingStage, PaintingMaterialRequirement,
    Material, Part,
)
from inventory.models import (
    RawMaterial, RawMaterialCategory, StockMovement, MaterialIssue,
)
from inventory import services


class PaintConsumptionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('pcuser', password='testpass')
        cls.cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.paint_raw = RawMaterial.objects.create(
            category=cls.cat, name='رنگ', code='PC1', unit='lit', pack_size=4,
        )
        cls.cat2 = RawMaterialCategory.objects.create(name=' مواد')
        cls.mon_raw = RawMaterial.objects.create(
            category=cls.cat2, name='ماده مونتاژ', code='MC1', unit='kg',
        )
        cls.material = Material.objects.create(
            name='ماده فرمول', thickness=Decimal('1'), raw_material=cls.mon_raw,
            consumption_per_unit=Decimal('1'),
        )
        cls.part = Part.objects.create(
            material=cls.material, name='قطعه', length=Decimal('1'), width=Decimal('1'),
            pname='محصول', routing_code='test',
        )
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='PC1')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        cls.process = PaintingProcess.objects.create(name='روند', code='PC', color_codes=['8'], is_active=True)
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='دهان', duration_minutes=30,
            drying_time_minutes=60, required_skill='painter',
        )
        cls.req = PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.paint_raw, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.5'),
        )

    def setUp(self):
        StockMovement.objects.create(raw_material=self.paint_raw, movement_type='purchase', quantity=Decimal('100'), note='م-existing')
        StockMovement.objects.create(raw_material=self.mon_raw, movement_type='purchase', quantity=Decimal('100'), note='م-existing')

    def test_paint_task_completion_no_movement(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='pending', painting_stage=self.stage, color_part='بدنه',
            step_order=1,
        )
        before = StockMovement.objects.count()
        task.status = 'done'
        task.save()
        self.assertEqual(StockMovement.objects.count(), before)

    def test_non_paint_task_completion_still_records(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, part=self.part,
            station_name='mon', quantity=2, status='pending', step_order=1,
        )
        before = StockMovement.objects.count()
        task.status = 'done'
        task.save()
        self.assertGreater(StockMovement.objects.count(), before)

    def test_handover_records_consumption_with_order_item_ref(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='pending', painting_stage=self.stage, color_part='بدنه',
            step_order=1,
        )
        auto_create_material_issues([task], requested_by=self.user)
        issue = MaterialIssue.objects.get(order_item=self.order_item, raw_material=self.paint_raw)
        services.execute_handover(
            issued_by=self.user, received_by=self.user,
            items=[(issue.id, Decimal('1'))], note='تحویل'
        )
        issue.refresh_from_db()
        movement = issue.stock_movement
        self.assertIsNotNone(movement)
        self.assertEqual(movement.reference_order_item, self.order_item)
        self.assertEqual(movement.quantity, Decimal('1'))

    def test_report_command_detects_covered_and_no_delete(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='pending', painting_stage=self.stage, color_part='بدنه',
            step_order=1,
        )
        auto_create_material_issues([task], requested_by=self.user)
        issue = MaterialIssue.objects.get(order_item=self.order_item, raw_material=self.paint_raw)
        services.execute_handover(
            issued_by=self.user, received_by=self.user,
            items=[(issue.id, Decimal('1'))], note='تحویل'
        )
        StockMovement.objects.create(
            raw_material=self.paint_raw, movement_type='consumption',
            quantity=Decimal('1'), reference_task=task, created_by=self.user,
            note='مصرف خودکار نقاشی - تسک #{}'.format(task.id),
        )
        out = StringIO()
        call_command('report_paint_auto_consumption', stdout=out)
        output = out.getvalue()
        self.assertIn('covered', output)
        self.assertEqual(StockMovement.objects.filter(note__startswith='مصرف خودکار نقاشی - تسک #').count(), 1)