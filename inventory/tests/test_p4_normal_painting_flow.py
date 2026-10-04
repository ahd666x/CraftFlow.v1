"""
P4 — یکپارچه‌سازی مسیر مواد «نقاشی عادی».

مسیر عملیاتی مجاز و یکتا:
    DailyMaterialQueue → execute_daily_delivery → execute_daily_return

MaterialIssue برای rework/defect و برای ایستگاه‌های غیرنقاشی فعال می‌ماند؛
فقط شاخهٔ «نقاشی عادی» از مسیر تولید issue خارج شده است.

سناریوی مرجع این ماژول (بخش ۳):
    planned 3 → pack 4 → delivery 4 → return 2 → actual 2
    انبار: −4 سپس +2 ⇒ خالص −2، و actual_consumption = 2
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from inventory import services
from inventory.models import (
    CustodyConsumption,
    DailyMaterialQueue,
    MaterialCustody,
    MaterialIssue,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
)
from product.models import (
    Color,
    Customer,
    Order,
    OrderItem,
    PaintingMaterialRequirement,
    PaintingProcess,
    PaintingStage,
    Product,
    ProductCategory,
    ProductionDefect,
    ProductionTask,
)
from product.utils import auto_create_material_issues


class P4Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_superuser('p4manager', password='pw')
        cls.worker = User.objects.create_user('p4worker', password='pw')

        cls.category = RawMaterialCategory.objects.create(name='مواد P4')
        # بستهٔ ۴ کیلویی: برای سناریوی planned 3 ⇒ delivery 4
        cls.raw_pack4 = RawMaterial.objects.create(
            category=cls.category, name='رنگ بسته‌ای P4', code='P4-P4',
            unit='kg', pack_size=Decimal('4'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته P4')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول P4', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند P4', code='P4', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='مرحله ۱ P4',
            duration_minutes=30, drying_time_minutes=0,
        )
        PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.raw_pack4,
            product=cls.product, color_part='بدنه',
            consumption_per_unit=Decimal('3.000'),
        )

        cls.customer = Customer.objects.create(name='مشتری P4', phone='09120000003')
        cls.order = Order.objects.create(
            user=cls.manager, customer=cls.customer, number='P4-1',
        )
        cls.day = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.day, time(8, 0))
        )

    def _paint_task(self, color_part='بدنه', quantity=1):
        item = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=quantity,
        )
        Color.objects.create(part=color_part, code='8', orderitem=item)
        return ProductionTask.objects.create(
            order=self.order, order_item=item, station_name='paint',
            step_order=1, quantity=quantity, status='pending',
            painting_stage=self.stage, color_part=color_part,
            assigned_worker=self.worker, scheduled_start=self.day_start,
        )

    def _queue(self):
        return DailyMaterialQueue.objects.get(
            work_date=self.day, worker=self.worker, raw_material=self.raw_pack4,
        )

    def _opening_stock(self, quantity='100'):
        StockMovement.objects.create(
            raw_material=self.raw_pack4, movement_type='purchase',
            quantity=Decimal(quantity), created_by=self.manager,
        )
        self.raw_pack4.refresh_from_db()
        return self.raw_pack4.current_stock

    def _setup_deliverable(self, task_quantity=1):
        task = self._paint_task(quantity=task_quantity)
        queue = self._queue()
        self._opening_stock()
        return task, queue


# ---------------------------------------------------------------------------
# 1) Delivery  ·  2) Return  ·  3) Over-return  ·  7) Stock invariant
# ---------------------------------------------------------------------------
class DailyQueueAccountingTests(P4Base):
    def test_01_planned_3_pack_4_delivers_4(self):
        """نیاز ۳ کیلو با بستهٔ ۴ کیلویی ⇒ تحویل فیزیکی ۴ کیلو."""
        _task, queue = self._setup_deliverable()
        self.assertEqual(queue.planned_quantity, Decimal('3.000'))

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('4.000'))
        self.assertEqual(queue.status, 'delivered')
        movement = StockMovement.objects.filter(
            raw_material=self.raw_pack4, movement_type='consumption',
        ).get()
        self.assertEqual(movement.quantity, Decimal('4.000'))

    def test_02_delivery_4_return_2_actual_2_and_stock_invariant(self):
        """تحویل ۴، برگشت ۲ ⇒ مصرف واقعی ۲ و موجودی = آغازین − ۴ + ۲."""
        _task, queue = self._setup_deliverable()
        opening = self.raw_pack4.current_stock

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.manager,
            returned_quantity=Decimal('2.000'),
        )

        queue.refresh_from_db()
        self.raw_pack4.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('4.000'))
        self.assertEqual(queue.returned_quantity, Decimal('2.000'))
        self.assertEqual(queue.actual_consumption, Decimal('2.000'))
        self.assertEqual(queue.excess_consumption, Decimal('0.000'))
        # closing = opening - delivery + return
        self.assertEqual(self.raw_pack4.current_stock, opening - Decimal('2.000'))

    def test_03_over_return_is_rejected(self):
        _task, queue = self._setup_deliverable()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        with self.assertRaises(services.HandoverError):
            services.execute_daily_return(
                queue_id=queue.pk, returned_by=self.manager,
                returned_quantity=Decimal('5.000'),
            )

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.000'))
        self.assertEqual(queue.actual_consumption, Decimal('4.000'))

    def test_04_double_delivery_is_rejected(self):
        _task, queue = self._setup_deliverable()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        with self.assertRaises(services.HandoverError):
            services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw_pack4, movement_type='consumption',
            ).count(),
            1,
        )

    def test_05_return_creates_only_stock_movement_return(self):
        """هیچ movement_type جدیدی وارد نمی‌شود."""
        _task, queue = self._setup_deliverable()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.manager,
            returned_quantity=Decimal('2.000'),
        )

        types = set(StockMovement.objects.values_list('movement_type', flat=True))
        self.assertTrue(types <= {'purchase', 'consumption', 'return'})
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw_pack4, movement_type='return',
            ).count(),
            1,
        )

    def test_06_daily_return_creates_no_worker_custody(self):
        """ماده برگشتی دوباره موجودی عمومی است، نه امانت کارگر."""
        _task, queue = self._setup_deliverable()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.manager,
            returned_quantity=Decimal('2.000'),
        )

        self.assertFalse(
            MaterialCustody.objects.filter(
                raw_material=self.raw_pack4, held_by=self.worker,
            ).exists()
        )
        self.assertFalse(CustodyConsumption.objects.exists())


# ---------------------------------------------------------------------------
# 5) Rework isolation  ·  6) Normal painting isolation
# ---------------------------------------------------------------------------
class EngineIsolationTests(P4Base):
    def test_07_normal_painting_creates_no_material_issue(self):
        """نقاشی عادی دیگر از Engine A درخواست نمی‌سازد."""
        task = self._paint_task()
        result = auto_create_material_issues([task], requested_by=self.manager)

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['deferred_to_daily_queue'], 1)
        self.assertEqual(
            MaterialIssue.objects.filter(order_item=task.order_item).count(), 0,
        )
        # و نیاز از صف روزانه آمده است.
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('3.000'))
        self.assertEqual(queue.sources.count(), 1)

    def test_08_rework_material_issue_is_still_created_and_delivered(self):
        """مسیر rework/خرابی دست‌نخورده و فعال است."""
        task = self._paint_task()
        defect = ProductionDefect.objects.create(
            order=self.order, order_item=task.order_item, color_part='بدنه',
            quantity=1, description='خط P4', reported_by=self.manager,
        )
        issue = MaterialIssue.objects.create(
            order_item=task.order_item, defect=defect, raw_material=self.raw_pack4,
            requested_quantity=Decimal('3.000'), purpose='rework',
            status='requested', requested_by=self.manager,
        )
        self._opening_stock()

        services.execute_handover(
            issued_by=self.manager, received_by=self.worker,
            items=[(issue.pk, Decimal('3.000'))],
        )

        issue.refresh_from_db()
        defect.refresh_from_db()
        self.assertEqual(issue.status, 'issued')
        self.assertEqual(defect.status, 'rework_issued')
        self.assertEqual(issue.movements.get().movement_type, 'consumption')

    def test_09_rework_purpose_still_creates_paint_material_issues(self):
        """اگر purpose='rework' صدا زده شود، شاخهٔ قدیمی هنوز کار می‌کند."""
        task = self._paint_task()
        result = auto_create_material_issues(
            [task], requested_by=self.manager, purpose='rework',
        )

        self.assertEqual(result['created'], 1)
        issue = MaterialIssue.objects.get(
            order_item=task.order_item, raw_material=self.raw_pack4,
        )
        self.assertEqual(issue.purpose, 'rework')
        self.assertEqual(issue.requested_quantity, Decimal('3.000'))

    def test_10_non_paint_stations_still_create_material_issues(self):
        """ایستگاه‌های غیرنقاشی همچنان از MaterialIssue استفاده می‌کنند."""
        raw = RawMaterial.objects.create(
            category=self.category, name='ماده ایستگاه P4', code='P4-ST',
            unit='kg',
        )
        task = ProductionTask.objects.create(
            order=self.order, station_name='cut', step_order=1, quantity=2,
            status='pending', assigned_worker=self.worker,
            scheduled_start=self.day_start,
        )
        MaterialIssue.objects.create(
            task=task, raw_material=raw, requested_quantity=Decimal('2.000'),
            purpose='production', status='requested', requested_by=self.manager,
        )

        result = auto_create_material_issues([task], requested_by=self.manager)
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['deferred_to_daily_queue'], 0)
        # رکورد غیرنقاشیِ موجود دست‌نخورده است و صف روزانه‌ای هم ساخته نشده.
        self.assertEqual(
            MaterialIssue.objects.filter(task=task).count(), 1,
        )
        self.assertFalse(
            DailyMaterialQueue.objects.filter(raw_material=raw).exists()
        )

    def test_11_legacy_paint_issues_are_never_deleted(self):
        """دادهٔ تاریخی Engine A می‌ماند؛ فقط رکورد جدید ساخته نمی‌شود."""
        task = self._paint_task()
        legacy = MaterialIssue.objects.create(
            order_item=task.order_item, color_part='بدنه',
            painting_process=self.process, raw_material=self.raw_pack4,
            requested_quantity=Decimal('3.000'), purpose='production',
            status='requested', requested_by=self.manager,
        )

        auto_create_material_issues([task], requested_by=self.manager)

        legacy.refresh_from_db()
        self.assertEqual(legacy.status, 'requested')
        self.assertEqual(MaterialIssue.objects.count(), 1)