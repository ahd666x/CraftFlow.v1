"""
P3.5 — traceability برای Engine B (``DailyMaterialQueue``) در گزارش مصرف مواد.

مسیر هدف:
    DailyMaterialQueue → DailyMaterialQueueSource → ProductionTask
        → OrderItem / PaintingStage / ColorPart
        → report_material_consumption

قاعدهٔ اصلی: هرگز انتساب جعلی ساخته نمی‌شود. یک ردیف صف یک حرکت aggregate
است و به هیچ تسک خاصی نسبت داده نمی‌شود؛ سهم هر منبع از نسبت سهمش به مجموع
سهم‌های همان صف به‌دست می‌آید و جمعش دقیقاً مصرف واقعی است.

این تست‌ها روی همان تابع واقعی گزارش (``_daily_queue_consumption_lines``) کار
می‌کنند، نه روی کپی‌ای از منطق آن.
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory import services
from inventory.models import (
    DailyMaterialQueue,
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
from product.views import _daily_queue_consumption_lines

REPORT_URL = 'report_material_consumption'


def _lines_for(queue):
    """خطوط مصرف Engine B که به همین صف (ماده و تاریخ آن) مربوط‌اند."""
    return [
        line for line in _daily_queue_consumption_lines()
        if line['raw_material'].pk == queue.raw_material_id
    ]


class QueueTraceabilityBase(TestCase):
    """دو محصول مستقل تا فرمول موادشان با هم قاطی نشود."""

    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_superuser('p35manager', password='pw')
        cls.worker = User.objects.create_user('p35worker', password='pw')

        cls.category = RawMaterialCategory.objects.create(name='مواد P3.5')
        cls.raw = RawMaterial.objects.create(
            category=cls.category, name='رنگ P3.5', code='P35-A',
            unit='lit', pack_size=Decimal('0'),
        )
        cls.raw_pack4 = RawMaterial.objects.create(
            category=cls.category, name='رنگ بسته‌ای P3.5', code='P35-B',
            unit='lit', pack_size=Decimal('4'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته P3.5')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول P3.5', base_price=1000,
        )
        cls.pack_product = Product.objects.create(
            category=cls.product_category, name='محصول بسته‌ای P3.5', base_price=1000,
        )

        cls.process = PaintingProcess.objects.create(
            name='روند P3.5', code='P35', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='مرحله ۱ P3.5',
            duration_minutes=30, drying_time_minutes=0,
        )
        cls.stage2 = PaintingStage.objects.create(
            process=cls.process, order=2, name='مرحله ۲ P3.5',
            duration_minutes=30, drying_time_minutes=0,
        )
        for product, raw in ((cls.product, cls.raw), (cls.pack_product, cls.raw_pack4)):
            for part in ('بدنه', 'درب'):
                PaintingMaterialRequirement.objects.create(
                    process=cls.process, raw_material=raw, product=product,
                    color_part=part, consumption_per_unit=Decimal('2.000'),
                )

        cls.customer = Customer.objects.create(name='مشتری P3.5', phone='09120000002')
        cls.order = Order.objects.create(
            user=cls.manager, customer=cls.customer, number='P35-1',
        )
        cls.day = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.day, time(8, 0))
        )

    def setUp(self):
        self.client.force_login(self.manager)

    # -- کمک‌کننده‌ها ------------------------------------------------------
    def _make_item(self, color_part='بدنه', product=None, code='8'):
        item = OrderItem.objects.create(
            order=self.order, product=product or self.product, quantity=1,
        )
        Color.objects.create(part=color_part, code=code, orderitem=item)
        return item

    def _paint_task(self, item, color_part='بدنه', quantity=1, step_order=1,
                    stage=None):
        return ProductionTask.objects.create(
            order=self.order, order_item=item, station_name='paint',
            step_order=step_order, quantity=quantity, status='pending',
            painting_stage=stage or self.stage, color_part=color_part,
            assigned_worker=self.worker, scheduled_start=self.day_start,
        )

    def _queue(self, raw=None):
        return DailyMaterialQueue.objects.get(
            work_date=self.day, worker=self.worker, raw_material=raw or self.raw,
        )

    def _fund_stock(self, material, quantity='100'):
        return StockMovement.objects.create(
            raw_material=material, movement_type='purchase',
            quantity=Decimal(quantity), created_by=self.manager,
        )

    def _report_rows(self):
        response = self.client.get(reverse(REPORT_URL))
        self.assertEqual(response.status_code, 200)
        return response.context['report_rows']

    def _qty_for(self, report_rows, material, item):
        total = Decimal('0')
        for row in report_rows:
            row_item = row['order_item']
            if row_item is None or row_item.id != item.id:
                continue
            bucket = row['materials'].get(material.id)
            if bucket:
                total += bucket['qty']
        return total


# ---------------------------------------------------------------------------
# Test 1 — traceability تحویل Engine B
# ---------------------------------------------------------------------------
class EngineBTraceabilityTests(QueueTraceabilityBase):
    def test_01_delivery_is_traceable_through_queue_source(self):
        item = self._make_item()
        self._paint_task(item)
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        self._fund_stock(self.raw)

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(queue.actual_consumption, Decimal('2.00'))

        source = queue.sources.get()
        self.assertEqual(source.production_task.order_item, item)
        self.assertEqual(source.painting_stage.process, self.process)
        self.assertEqual(source.raw_material, self.raw)

        # حرکت انبار عمداً بدون reference است (aggregate).
        movement = StockMovement.objects.filter(
            movement_type='consumption', raw_material=self.raw,
        ).get()
        self.assertIsNone(movement.reference_task_id)
        self.assertIsNone(movement.reference_order_item_id)

        rows = self._report_rows()
        self.assertEqual(self._qty_for(rows, self.raw, item), Decimal('2.00'))

    def test_02_single_source_line_points_at_task_item_and_process(self):
        item = self._make_item()
        self._paint_task(item)
        queue = self._queue()
        self._fund_stock(self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        lines = _lines_for(queue)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['item'], item)
        self.assertEqual(lines[0]['process'], self.process)
        self.assertEqual(lines[0]['color_part'], 'بدنه')
        self.assertEqual(lines[0]['raw_material'], self.raw)
        self.assertEqual(lines[0]['quantity'], Decimal('2.00'))


# ---------------------------------------------------------------------------
# Test 2 — صف aggregate با چند منبع، بدون انتساب جعلی
# ---------------------------------------------------------------------------
class AggregateQueueAttributionTests(QueueTraceabilityBase):
    def test_03_multi_source_queue_is_split_by_share_not_by_one_task(self):
        item_a = self._make_item(color_part='بدنه')
        item_b = self._make_item(color_part='درب')
        self._paint_task(item_a, color_part='بدنه', step_order=1)
        self._paint_task(item_b, color_part='درب', step_order=2)

        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('4.00'))
        self.assertEqual(queue.sources.count(), 2)
        self._fund_stock(self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        # یک حرکت aggregate؛ نه به تسک اول چسبانده می‌شود نه به دوم.
        movements = StockMovement.objects.filter(
            movement_type='consumption', raw_material=self.raw,
        )
        self.assertEqual(movements.count(), 1)
        movement = movements.get()
        self.assertEqual(movement.quantity, Decimal('4.00'))
        self.assertIsNone(movement.reference_task_id)

        lines = sorted(_lines_for(queue), key=lambda line: line['item'].id)
        self.assertEqual(len(lines), 2)
        self.assertEqual([line['item'] for line in lines], [item_a, item_b])
        self.assertEqual(
            [line['quantity'] for line in lines],
            [Decimal('2.00'), Decimal('2.00')],
        )
        self.assertEqual(sum(l['quantity'] for l in lines), Decimal('4.00'))

    def test_04_uneven_shares_are_allocated_proportionally(self):
        item_a = self._make_item(color_part='بدنه')
        item_b = self._make_item(color_part='درب')
        self._paint_task(item_a, color_part='بدنه', quantity=3, step_order=1)
        self._paint_task(item_b, color_part='درب', quantity=1, step_order=2)

        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('8.00'))
        self._fund_stock(self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        lines = sorted(_lines_for(queue), key=lambda line: line['item'].id)
        self.assertEqual([l['quantity'] for l in lines],
                         [Decimal('6.00'), Decimal('2.00')])

    def test_05_rounding_remainder_keeps_the_total_exact(self):
        """سه سهم مساوی و تحویل بسته‌ای: جمع سهم‌ها باید دقیقاً مصرف واقعی باشد."""
        items = [
            self._make_item(color_part='بدنه', product=self.pack_product)
            for _ in range(3)
        ]
        for index, item in enumerate(items):
            self._paint_task(item, color_part='بدنه', step_order=index + 1)

        queue = self._queue(raw=self.raw_pack4)
        self.assertEqual(queue.planned_quantity, Decimal('6.00'))
        self.assertEqual(queue.sources.count(), 3)
        self._fund_stock(self.raw_pack4)

        # بستهٔ ۴ تایی ⇒ تحویل فیزیکی ۸ (نیاز ۶، گرد به بالا).
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)
        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('8.00'))

        lines = _lines_for(queue)
        self.assertEqual(len(lines), 3)
        self.assertEqual(sum(l['quantity'] for l in lines), Decimal('8.00'))
        self.assertEqual(
            sorted(l['quantity'] for l in lines),
            [Decimal('2.66'), Decimal('2.67'), Decimal('2.67')],
        )

    def test_06_queue_without_sources_creates_no_attribution(self):
        item = self._make_item()
        self._paint_task(item)
        queue = self._queue()
        queue.sources.all().delete()
        self._fund_stock(self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        self.assertEqual(_lines_for(queue), [])
        # ولی رکورد تحویل و مصرف واقعی سر جایش هست.
        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('2.00'))


# ---------------------------------------------------------------------------
# Test 3 — گزارش هر دو engine را بدون duplicate می‌بیند
# ---------------------------------------------------------------------------
class BothEnginesInReportTests(QueueTraceabilityBase):
    def _engine_a_movement(self, item, quantity='1.00'):
        return StockMovement.objects.create(
            raw_material=self.raw, movement_type='consumption',
            quantity=Decimal(quantity), reference_order_item=item,
            reference_color_part='بدنه', created_by=self.manager,
            note='تحویل انبار — نقاشی',
        )

    def test_07_report_contains_engine_a_and_engine_b_together(self):
        item = self._make_item()
        self._paint_task(item)
        queue = self._queue()
        self._fund_stock(self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)
        self._engine_a_movement(item, '1.00')

        rows = self._report_rows()
        # 1 (Engine A) + 2 (Engine B) در همان ردیف گزارش، بدون تکرار
        self.assertEqual(self._qty_for(rows, self.raw, item), Decimal('3.00'))

    def test_08_each_row_contributes_exactly_once(self):
        item = self._make_item()
        self._paint_task(item)
        queue = self._queue()
        self._fund_stock(self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)
        self._engine_a_movement(item, '1.00')

        movements = StockMovement.objects.filter(
            raw_material=self.raw, movement_type='consumption',
        )
        self.assertEqual(movements.count(), 2)
        referenced = movements.filter(reference_order_item__isnull=False)
        self.assertEqual(referenced.count(), 1)
        self.assertEqual(referenced.get().quantity, Decimal('1.00'))

        rows = self._report_rows()
        self.assertEqual(self._qty_for(rows, self.raw, item), Decimal('3.00'))

    def test_09_pending_queue_reports_no_consumption(self):
        item = self._make_item()
        self._paint_task(item)
        self.assertEqual(self._queue().actual_consumption, Decimal('0'))
        self.assertEqual(
            self._qty_for(self._report_rows(), self.raw, item), Decimal('0'),
        )


# ---------------------------------------------------------------------------
# Test 4 — return در مصرف واقعی
# ---------------------------------------------------------------------------
class ReturnAccountingTests(QueueTraceabilityBase):
    def _deliver(self):
        item = self._make_item()
        self._paint_task(item)
        queue = self._queue()
        self._fund_stock(self.raw)
        return item, queue

    def test_10_return_reduces_reported_consumption(self):
        item, queue = self._deliver()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)
        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('2.00'))
        self.assertEqual(
            self._qty_for(self._report_rows(), self.raw, item), Decimal('2.00'),
        )

        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.manager,
            returned_quantity=Decimal('1.00'),
        )

        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('2.00'))
        self.assertEqual(queue.returned_quantity, Decimal('1.00'))
        self.assertEqual(queue.actual_consumption, Decimal('1.00'))
        # نباید 2 (کل تحویل) گزارش شود.
        self.assertEqual(
            self._qty_for(self._report_rows(), self.raw, item), Decimal('1.00'),
        )

    def test_11_full_return_reports_no_consumption(self):
        item, queue = self._deliver()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.manager,
            returned_quantity=Decimal('2.00'),
        )

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'returned')
        self.assertEqual(queue.actual_consumption, Decimal('0'))
        self.assertEqual(
            self._qty_for(self._report_rows(), self.raw, item), Decimal('0'),
        )

    def test_12_partial_returns_do_not_stack(self):
        item, queue = self._deliver()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.manager,
            returned_quantity=Decimal('0.50'),
        )
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.manager,
            returned_quantity=Decimal('0.50'),
        )

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('1.00'))
        self.assertEqual(queue.actual_consumption, Decimal('1.00'))
        self.assertEqual(
            self._qty_for(self._report_rows(), self.raw, item), Decimal('1.00'),
        )


# ---------------------------------------------------------------------------
# Test 5 — regression مسیر rework و Engine A
# ---------------------------------------------------------------------------
class ReworkRegressionTests(QueueTraceabilityBase):
    def _rework(self, item):
        defect = ProductionDefect.objects.create(
            order=self.order, order_item=item, color_part='بدنه',
            quantity=1, description='خط P3.5', reported_by=self.manager,
        )
        issue = MaterialIssue.objects.create(
            order_item=item, defect=defect, raw_material=self.raw,
            requested_quantity=Decimal('1.00'), purpose='rework',
            status='requested', requested_by=self.manager,
        )
        return defect, issue

    def test_13_rework_issue_delivery_is_unchanged(self):
        item = self._make_item()
        defect, issue = self._rework(item)
        self._fund_stock(self.raw)

        services.execute_handover(
            issued_by=self.manager, received_by=self.worker,
            items=[(issue.pk, Decimal('1.00'))],
        )

        issue.refresh_from_db()
        defect.refresh_from_db()
        self.assertEqual(issue.status, 'issued')
        self.assertEqual(issue.issued_quantity, Decimal('1.00'))
        self.assertEqual(defect.status, 'rework_issued')

        movement = issue.movements.get()
        self.assertEqual(movement.movement_type, 'consumption')
        self.assertEqual(movement.fulfilled_issue, issue)
        self.assertEqual(movement.raw_material, self.raw)

    def test_14_rework_movement_is_excluded_from_the_report_as_before(self):
        item = self._make_item()
        _defect, issue = self._rework(item)
        self._fund_stock(self.raw)
        services.execute_handover(
            issued_by=self.manager, received_by=self.worker,
            items=[(issue.pk, Decimal('1.00'))],
        )

        rows = self._report_rows()
        self.assertEqual(self._qty_for(rows, self.raw, item), Decimal('0'))
        defect_rows = [r for r in rows if r['defect_count']]
        self.assertTrue(defect_rows)
        self.assertEqual(defect_rows[0]['defect_count'], 1)

    def test_15_auto_create_material_issues_defers_normal_painting(self):
        """P4: شاخهٔ نقاشی عادی دیگر issue نمی‌سازد؛ نیاز از صف روزانه می‌آید.

        مسیر rework (purpose='rework') و ایستگاه‌های غیرنقاشی دست‌نخورده‌اند
        (test_13 و test_10 در ماژول P4).
        """
        from product.utils import auto_create_material_issues

        item = self._make_item()
        task = self._paint_task(item)
        result = auto_create_material_issues([task], requested_by=self.manager)

        # نیاز نقاشی عادی به صف مواد روزانه منتقل شده است، نه به MaterialIssue.
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['deferred_to_daily_queue'], 1)
        self.assertEqual(MaterialIssue.objects.count(), 0)
        # همان نیاز از طریق صف روزانه قابل دسترس است.
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        self.assertEqual(queue.sources.get().production_task, task)


# ---------------------------------------------------------------------------
# Test 6 — P1/P2 regression
# ---------------------------------------------------------------------------
class P1P2RegressionTests(QueueTraceabilityBase):
    def test_16_p1_dedup_and_p2_material_stay_intact(self):
        from product.utils import get_resolved_painting_requirements_for_task

        item = self._make_item()
        task1 = self._paint_task(item, step_order=1)
        task2 = self._paint_task(item, step_order=2, stage=self.stage2)

        # P2: ماده resolve‌شده همان FK ذخیره‌شده است.
        resolved, errors, _ctx = get_resolved_painting_requirements_for_task(task1)
        self.assertEqual(errors, [])
        self.assertEqual([e['raw_material'] for e in resolved], [self.raw])

        # P1: دو stage از یک work unit ⇒ یک نیاز، با هر دو task به‌عنوان منبع.
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        self.assertEqual(queue.sources.count(), 2)
        self.assertEqual(
            {s.production_task_id for s in queue.sources.all()},
            {task1.id, task2.id},
        )

        self._fund_stock(self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.manager)

        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('2.00'))
        lines = _lines_for(queue)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['item'], item)
        self.assertEqual(lines[0]['quantity'], Decimal('2.00'))