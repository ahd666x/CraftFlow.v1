"""
P5 — traceability کامل مصرف واقعی نقاشی عادی (Engine B).

ماژول صرفاً خواندنی است: هیچ مسیر عملیاتی، داده یا مدلی تغییر نمی‌کند.
زنجیرهٔ مورد بررسی:

    StockMovement → DailyMaterialQueue → DailyMaterialQueueSource
    → ProductionTask → PaintingStage → PaintingProcess
    → OrderItem → Order / Product → ColorPart (مقدار موجود)

قرارداد مصرف:
    planned 3 → delivery 3 → return 0.5 → actual 2.5
    سهم هر source با همان فرمول P3.5 (سهم نسبی + Largest Remainder).
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
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
from product.views import daily_queue_consumption_trace


class P5Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_superuser('p5manager', password='pw')
        cls.worker = User.objects.create_user('p5worker', password='pw')

        cls.category = RawMaterialCategory.objects.create(name='مواد P5')
        # بدون بسته: تحویل فیزیکی = نیاز برنامه‌ریزی‌شده
        cls.raw = RawMaterial.objects.create(
            category=cls.category, name='رنگ P5', code='P5-A',
            unit='kg', pack_size=Decimal('0'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته P5')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول P5', base_price=1000,
        )
        cls.product_b = Product.objects.create(
            category=cls.product_category, name='محصول P5 ب', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند P5', code='P5', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='مرحله ۱ P5',
            duration_minutes=30, drying_time_minutes=0,
        )
        cls.stage2 = PaintingStage.objects.create(
            process=cls.process, order=2, name='مرحله ۲ P5',
            duration_minutes=30, drying_time_minutes=0,
        )
        # مصرف ۱ کیلو به ازای هر واحد — حفظاری سهم‌ها ساده می‌شود
        for product in (cls.product, cls.product_b):
            for part in ('بدنه', 'درب'):
                PaintingMaterialRequirement.objects.create(
                    process=cls.process, raw_material=cls.raw, product=product,
                    color_part=part, consumption_per_unit=Decimal('1.000'),
                )

        cls.customer = Customer.objects.create(name='مشتری P5', phone='09120000004')
        cls.day = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.day, time(8, 0))
        )

    def _item(self, product=None):
        order = Order.objects.create(
            user=self.manager, customer=self.customer,
            number=f'P5-{Order.objects.count() + 1}',
        )
        item = OrderItem.objects.create(
            order=order, product=product or self.product, quantity=1,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=item)
        return item

    def _paint_task(self, item, color_part='بدنه', stage=None, step_order=1):
        return ProductionTask.objects.create(
            order=item.order, order_item=item, station_name='paint',
            step_order=step_order, quantity=1, status='pending',
            painting_stage=stage or self.stage, color_part=color_part,
            assigned_worker=self.worker, scheduled_start=self.day_start,
        )

    def _queue(self):
        return DailyMaterialQueue.objects.get(
            work_date=self.day, worker=self.worker, raw_material=self.raw,
        )

    def _fund_stock(self, material=None, quantity='100'):
        material = material or self.raw
        StockMovement.objects.create(
            raw_material=material, movement_type='purchase',
            quantity=Decimal(quantity), created_by=self.manager,
        )
        material.refresh_from_db()


class ConsumptionTraceTests(P5Base):
    def test_01_single_source_full_trace(self):
        """یک مصرف → یک source → trace کامل با حرکت تحویل."""
        item = self._item()
        self._paint_task(item)
        self._fund_stock()
        queue = self._queue()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)

        trace = daily_queue_consumption_trace(queue)

        self.assertEqual(trace['movement'].movement_type, 'consumption')
        self.assertEqual(trace['movement'].quantity, Decimal('1.00'))
        self.assertEqual(trace['raw_material'], self.raw)
        self.assertEqual(trace['actual_consumption'], Decimal('1.00'))
        self.assertEqual(trace['work_date'], self.day)
        self.assertEqual(trace['worker'], self.worker)
        self.assertEqual(trace['queue'], queue)

        self.assertEqual(len(trace['sources']), 1)
        src = trace['sources'][0]
        self.assertEqual(src['production_task'].order_item, item)
        self.assertEqual(src['painting_stage'], self.stage)
        self.assertEqual(src['painting_process'], self.process)
        self.assertEqual(src['order_item'], item)
        self.assertEqual(src['order'], item.order)
        self.assertEqual(src['product'], self.product)
        self.assertEqual(src['color_part'], 'بدنه')
        self.assertEqual(src['source_quantity'], Decimal('1.00'))
        self.assertEqual(src['allocated_quantity'], Decimal('1.00'))

    def test_02_multiple_sources_all_preserved(self):
        """صف aggregate: همهٔ sourceها دیده می‌شوند، به هیچ‌کدام نسبت نمی‌خورد.

        دو بخش رنگیِ یک آیتم = دو واحد کار نقاشی (P1: نیاز به ازای هر
        (آیتم، بخش رنگی، روند، ماده) یک‌بار شمرده می‌شود).
        """
        item = self._item()
        Color.objects.create(part='درب', code='8', orderitem=item)
        self._paint_task(item, color_part='بدنه', step_order=1)
        self._paint_task(item, color_part='درب', stage=self.stage2, step_order=2)
        self._fund_stock()
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)

        trace = daily_queue_consumption_trace(queue)
        self.assertEqual(len(trace['sources']), 2)
        stages = {s['painting_stage'] for s in trace['sources']}
        self.assertEqual(stages, {self.stage, self.stage2})
        parts = {s['color_part'] for s in trace['sources']}
        self.assertEqual(parts, {'بدنه', 'درب'})
        # حرکت aggregate به هیچ تسک/آیتم واحدی ارجاع ندارد.
        self.assertIsNone(trace['movement'].reference_task)
        self.assertIsNone(trace['movement'].reference_order_item)
        for src in trace['sources']:
            self.assertEqual(src['allocated_quantity'], Decimal('1.00'))

    def test_03_multiple_orders_in_one_queue(self):
        """دو سفارش مختلف در یک صف aggregate — هر دو قابل trace هستند."""
        item_a = self._item(product=self.product)
        item_b = self._item(product=self.product_b)
        self._paint_task(item_a)
        self._paint_task(item_b)
        self._fund_stock()
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)

        trace = daily_queue_consumption_trace(queue)
        orders = {s['order'] for s in trace['sources']}
        self.assertEqual(orders, {item_a.order, item_b.order})
        products = {s['product'] for s in trace['sources']}
        self.assertEqual(products, {self.product, self.product_b})
        items = {s['order_item'] for s in trace['sources']}
        self.assertEqual(items, {item_a, item_b})

    def test_04_color_part_value_preserved(self):
        """color_part به‌عنوان مقدار موجود (CharField) trace می‌شود."""
        item = self._item()
        Color.objects.create(part='درب', code='8', orderitem=item)
        self._paint_task(item, color_part='درب')
        self._fund_stock()
        queue = self._queue()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)

        trace = daily_queue_consumption_trace(queue)
        self.assertEqual(trace['sources'][0]['color_part'], 'درب')

    def test_05_product_and_order_reachable(self):
        """از source به Product و Order (و مشتری آن) می‌رسیم."""
        item = self._item(product=self.product_b)
        self._paint_task(item)
        self._fund_stock()
        queue = self._queue()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)

        src = daily_queue_consumption_trace(queue)['sources'][0]
        self.assertEqual(src['order_item'], item)
        self.assertEqual(src['product'], self.product_b)
        self.assertEqual(src['order'], item.order)
        self.assertEqual(src['order'].customer, self.customer)

    def test_06_quantity_conservation_largest_remainder(self):
        """Σ سهم‌ها == مصرف واقعی، با همان Largest Remainder پیشنهادی."""
        item_a = self._item()
        Color.objects.create(part='درب', code='8', orderitem=item_a)
        item_b = self._item(product=self.product_b)
        # سه واحد کار نقاشی یکتا: (A، بدنه)، (A، درب)، (B، بدنه)
        self._paint_task(item_a, color_part='بدنه', step_order=1)
        self._paint_task(item_a, color_part='درب', stage=self.stage2, step_order=2)
        self._paint_task(item_b, color_part='بدنه', stage=self.stage2, step_order=3)
        self._fund_stock()
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('3.00'))
        self.assertEqual(len(queue.sources.all()), 3)

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.manager,
            returned_quantity=Decimal('0.50'),
        )

        trace = daily_queue_consumption_trace(queue)
        self.assertEqual(trace['actual_consumption'], Decimal('2.50'))
        self.assertEqual(len(trace['sources']), 3)
        total = sum(s['allocated_quantity'] for s in trace['sources'])
        self.assertEqual(total, Decimal('2.50'))
        # 2.50 روی ۳ سهم برابر: ۰٫۸۳ + ۰٫۸۳ + ۰٫۸۴ (باقی‌مانده به بزرگ‌ترین)
        values = sorted(
            (s['allocated_quantity'] for s in trace['sources']),
            reverse=True,
        )
        self.assertEqual(
            values, [Decimal('0.84'), Decimal('0.83'), Decimal('0.83')])
        # حرکت برگشتی هم از طریق ارجاع صف trace می‌شود.
        self.assertEqual(len(trace['return_movements']), 1)
        self.assertEqual(trace['return_movements'][0].quantity, Decimal('0.50'))

    def test_07_queue_without_sources_no_fabrication(self):
        """صف بدون source: هیچ trace جعلی ساخته نمی‌شود؛ مصرف حذف نمی‌شود."""
        queue = DailyMaterialQueue.objects.create(
            work_date=self.day, worker=self.worker, raw_material=self.raw,
            planned_quantity=Decimal('2.00'), status='pending',
        )
        self._fund_stock()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)

        trace = daily_queue_consumption_trace(queue)
        self.assertEqual(trace['actual_consumption'], Decimal('2.00'))
        self.assertEqual(trace['sources'], [])
        # مصرف واقعی جعل/حذف نشده: حرکت تحویل همچنان موجود و قابل trace است.
        self.assertEqual(trace['movement'].quantity, Decimal('2.00'))

        # تحویل با note سفارشی: پیوند صف در note نیست، پس حرکت منتسب
        # نمی‌شود (None) — هیچ گمانه‌زنی تاریخی/مقداری انجام نمی‌شود.
        raw_note = RawMaterial.objects.create(
            category=self.category, name='رنگ P5 نوت', code='P5-N',
            unit='kg', pack_size=Decimal('0'),
        )
        queue2 = DailyMaterialQueue.objects.create(
            work_date=self.day, worker=self.worker, raw_material=raw_note,
            planned_quantity=Decimal('1.00'), status='pending',
        )
        self._fund_stock(material=raw_note)
        services.execute_daily_delivery(
            queue_id=queue2.pk, delivered_by=self.manager,
            note='بدون ارجاع به صف',
        )
        trace2 = daily_queue_consumption_trace(queue2)
        self.assertIsNone(trace2['movement'])
        self.assertEqual(trace2['actual_consumption'], Decimal('1.00'))
        self.assertEqual(trace2['sources'], [])

    def test_08_rework_engine_a_isolation(self):
        """trace Engine A (rework) دست‌نخورده می‌ماند و با Engine B قاطی نمی‌شود."""
        item = self._item()
        self._paint_task(item)
        defect = ProductionDefect.objects.create(
            order=item.order, order_item=item, color_part='بدنه',
            quantity=1, description='خط P5', reported_by=self.manager,
        )
        issue = MaterialIssue.objects.create(
            order_item=item, defect=defect, raw_material=self.raw,
            requested_quantity=Decimal('1.00'), purpose='rework',
            status='requested', requested_by=self.manager,
            color_part='بدنه',
        )
        self._fund_stock()

        services.execute_handover(
            issued_by=self.manager, received_by=self.worker,
            items=[(issue.pk, Decimal('1.00'))],
        )

        # Engine A: حرکت به issue وصل است و زنجیرهٔ خودش را حفظ می‌کند.
        movement = StockMovement.objects.filter(
            movement_type='consumption', fulfilled_issue=issue).get()
        self.assertEqual(movement.reference_order_item, item)
        self.assertEqual(movement.reference_color_part, 'بدنه')

        # Engine B: trace صف نقاشی عادی، هیچ ردیف rework در آن قاطی نمی‌شود.
        queue = self._queue()
        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)
        trace = daily_queue_consumption_trace(queue)
        self.assertEqual(trace['movement'].fulfilled_issue, None)
        self.assertEqual(len(trace['sources']), 1)
        self.assertEqual(trace['sources'][0]['order_item'], item)
        # دو حرکت مصرف مجزا وجود دارد: یکی Engine A، یکی Engine B.
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw, movement_type='consumption').count(),
            2,
        )
        self.assertEqual(
            StockMovement.objects.filter(
                fulfilled_issue__isnull=False).count(),
            1,
        )
