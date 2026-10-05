from datetime import time

from django.test import TestCase
from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone
from io import StringIO
from decimal import Decimal

from product.utils import auto_create_material_issues
from product.views import daily_queue_consumption_trace
from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    Customer, Color, PaintingProcess, PaintingStage, PaintingMaterialRequirement,
    Material, Part,
)
from inventory.models import (
    DailyMaterialQueue, RawMaterial, RawMaterialCategory, StockMovement, MaterialIssue,
)
from inventory import services


class PaintConsumptionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('pcuser', password='testpass')
        cls.cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.paint_raw = RawMaterial.objects.create(
            category=cls.cat, name='رنگ', code='PC1', unit='lit',
            # بدون بسته: تحویل فیزیکی Engine B = نیاز برنامه‌ریزی‌شده
            # (گرد کردن بسته در تست‌های P4/phase4/phase5 پوشش داده است)
            pack_size=0,
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

        # Engine B: صف مواد روزانه برای تسک‌های نقاشیِ همین روز
        cls.day = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.day, time(8, 0))
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

    def test_daily_delivery_records_consumption_traceable_to_order_item(self):
        """P4: نیاز نقاشی عادی از صف مواد روزانه (Engine B) تحویل
        می‌شود. حرکت مصرف aggregate است و پیوند به آیتم سفارش از
        طریق زنجیرهٔ صف → منبع → تسک برقرار است."""
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='pending', painting_stage=self.stage, color_part='بدنه',
            step_order=1, assigned_worker=self.user, scheduled_start=self.day_start,
        )

        result = auto_create_material_issues([task], requested_by=self.user)
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['deferred_to_daily_queue'], 1)
        self.assertEqual(
            MaterialIssue.objects.filter(order_item=self.order_item).count(), 0,
        )

        queue = DailyMaterialQueue.objects.get(
            work_date=self.day, worker=self.user, raw_material=self.paint_raw,
        )
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))  # 2 * 0.5
        self.assertEqual(queue.sources.count(), 1)

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.user)

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'delivered')
        movement = StockMovement.objects.filter(
            raw_material=self.paint_raw, movement_type='consumption',
        ).get()
        self.assertIsNotNone(movement)
        self.assertEqual(movement.quantity, Decimal('1.00'))
        # حرکت Engine B عمداً هیچ reference مستقیم ندارد؛
        # traceability از note حرکت به صف و از منابع صف به تسک/آیتم است.
        self.assertIsNone(movement.reference_task)
        self.assertIsNone(movement.reference_order_item)

        trace = daily_queue_consumption_trace(queue)
        self.assertEqual(trace['movement'], movement)
        self.assertEqual(trace['actual_consumption'], Decimal('1.00'))
        self.assertEqual(trace['work_date'], self.day)
        self.assertEqual(trace['worker'], self.user)
        self.assertEqual(len(trace['sources']), 1)
        src = trace['sources'][0]
        self.assertEqual(src['production_task'], task)
        self.assertEqual(src['order_item'], self.order_item)
        self.assertEqual(src['color_part'], 'بدنه')
        self.assertEqual(src['painting_process'], self.process)
        self.assertEqual(src['source_quantity'], Decimal('1.00'))
        self.assertEqual(src['allocated_quantity'], Decimal('1.00'))

    def test_report_command_detects_covered_and_no_delete(self):
        """دستور گزارش مصرف خودکار نقاشی روی دادهٔ تاریخی (پیش از P4)
        کار می‌کند: حرکات «مصرف خودکار نقاشی - تسک #» که پوشش‌یافته
        توسط یک درخواست تولیدی صادر‌شده هستند را شناسایی می‌کند و در
        حالت پیش‌فرض هیچ‌کدام را حذف نمی‌کند.

        P4: نقاشی عادی دیگر MaterialIssue تولیدی نمی‌سازد، پس رکورد
        پوشش‌دهنده در اینجا به‌صورت دادهٔ تاریخی Engine A (پیش از P4)
        ساخته می‌شود — همان شکلی که این دستور گزارش برای آن نوشته
        شده (مانند تست legacy در P4)."""
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='paint',
            quantity=2, status='pending', painting_stage=self.stage, color_part='بدنه',
            step_order=1,
        )

        # رکورد تاریخی Engine A: درخواست تولیدی سطح آیتم (پیش از P4)
        legacy_issue = MaterialIssue.objects.create(
            order_item=self.order_item, color_part='بدنه',
            painting_process=self.process, raw_material=self.paint_raw,
            requested_quantity=Decimal('1'), purpose='production',
            status='requested', requested_by=self.user,
        )
        services.execute_handover(
            issued_by=self.user, received_by=self.user,
            items=[(legacy_issue.id, Decimal('1'))], note='تحویل'
        )
        legacy_issue.refresh_from_db()
        self.assertEqual(legacy_issue.status, 'issued')

        # حرکت مصرف خودکار نقاشی (دیگر توسط هیچ مسیر فعالی ساخته نمی‌شود)
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