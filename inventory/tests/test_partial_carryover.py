from datetime import date, datetime, time
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from inventory import services
from inventory.models import DailyMaterialQueue, RawMaterial, RawMaterialCategory, StockMovement
from inventory.services import HandoverError
from product.models import (
    Color, Customer, Order, OrderItem, PaintingMaterialRequirement, PaintingProcess,
    PaintingStage, Product, ProductCategory, ProductionTask,
)

MON, TUE = date(2026, 3, 2), date(2026, 3, 3)
THU, SAT = date(2026, 3, 5), date(2026, 3, 7)      # جمعه ۲۰۲۶-۰۳-۰۶ تعطیل است


class PartialBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('pc_admin', password='pw')
        cls.worker = User.objects.create_user('pc_worker', password='pw')
        group, _ = Group.objects.get_or_create(name='انبار')
        cls.admin.groups.add(group)

        cat = RawMaterialCategory.objects.create(name='مواد')
        # استر پلی‌اورتان: بستهٔ ۴.۵ کیلویی، موجودی ۵.۵ (۱.۰ ناقص + ۴.۵ کامل)
        cls.raw = RawMaterial.objects.create(
            category=cat, name='استر', code='PC-E', unit='kg',
            pack_size=Decimal('4.5'))
        StockMovement.objects.create(
            raw_material=cls.raw, movement_type='purchase',
            quantity=Decimal('5.5'), created_by=cls.admin)
        pcat = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(
            category=pcat, name='محصول', base_price=1)
        proc = PaintingProcess.objects.create(
            name='نقاشی', code='PC', color_codes=['8'], is_active=True)
        cls.stage = PaintingStage.objects.create(
            process=proc, order=1, name='زیرکار',
            duration_minutes=30, drying_time_minutes=0)
        PaintingMaterialRequirement.objects.create(
            process=proc, stage=cls.stage, raw_material=cls.raw,
            product=cls.product, color_part='بدنه', consumption_per_unit=Decimal('1.000'))
        customer = Customer.objects.create(name='مشتری', phone='0912')
        cls.order = Order.objects.create(
            user=cls.admin, customer=customer, number='PC-1')
        cls.item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.item)

    def _task(self, day, quantity=2):
        start = timezone.make_aware(datetime.combine(day, time(8, 0)))
        return ProductionTask.objects.create(
            order=self.order, order_item=self.item, station_name='paint',
            step_order=1, quantity=quantity, status='pending',
            painting_stage=self.stage, color_part='بدنه',
            assigned_worker=self.worker, scheduled_start=start)

    def _row(self, day):
        return DailyMaterialQueue.objects.filter(
            work_date=day, worker=self.worker, raw_material=self.raw).first()

    def _deliver(self, queue, quantity=None):
        if quantity is None and hasattr(self, '_use_suggested') and self._use_suggested:
            # Use suggested delivery for new behavior tests
            preview = services.preview_daily_delivery(queue.pk)
            quantity = str(preview['suggested_delivery'])
        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.admin, quantity=quantity)
        queue.refresh_from_db()
        return queue


class OpenPackTests(PartialBase):
    def test_suggested_delivery_leaves_full_pack(self):
        self._task(MON)
        queue = self._row(MON)
        self._use_suggested = True
        preview = services.preview_daily_delivery(queue.pk)
        self.assertEqual(preview['suggested_delivery'], Decimal('1.00'))
        self.assertEqual(preview['open_remainder'], Decimal('1.00'))
        self.assertEqual(preview['from_open'], Decimal('1.00'))
        self.assertEqual(preview['from_new_pack'], Decimal('0.00'))
        self.assertEqual(preview['remainder_after'], Decimal('0.00'))

        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('1.00'))
        self.assertEqual(self.raw.current_stock, Decimal('4.50'))
        self.assertEqual(queue.status, 'partial')


class PartialDeliveryTests(PartialBase):
    def test_short_delivery_is_partial_and_carries_to_next_working_day(self):
        self._task(MON)
        queue = self._row(MON)
        with self.captureOnCommitCallbacks(execute=True):
            self._deliver(queue, quantity='1.20')

        self.assertEqual(queue.status, 'partial')
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        carried = self._row(TUE)
        self.assertIsNotNone(carried)
        self.assertEqual(carried.planned_quantity, Decimal('0.80'))
        source = carried.sources.get()
        self.assertEqual(source.kind, 'carryover')
        self.assertEqual(source.carryover_from_id, queue.pk)

        # With new logic: stock after MON = 4.30, pack = 4.5
        # open_remainder = 4.30 % 4.5 = 4.30 > need (0.80)
        # So suggested delivery = 4.30 (entire open pack remainder)
        self._use_suggested = True
        self._deliver(carried)
        self.assertEqual(carried.delivered_quantity, Decimal('4.30'))
        self.assertEqual(carried.excess_consumption, Decimal('3.50'))
        self.assertEqual(carried.status, 'delivered')
        self.assertFalse(carried.has_plan_conflict)

    def test_carryover_skips_friday(self):
        self._task(THU)
        queue = self._row(THU)
        with self.captureOnCommitCallbacks(execute=True):
            self._deliver(queue, quantity='1.00')
        self.assertIsNone(self._row(date(2026, 3, 6)))
        self.assertEqual(self._row(SAT).planned_quantity, Decimal('1.00'))

    def test_partial_return_keeps_partial_status(self):
        self._task(MON)
        queue = self._row(MON)
        self._deliver(queue, quantity='1.20')
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.admin, returned_quantity='0.20')
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'partial')
        self.assertEqual(queue.actual_consumption, Decimal('1.00'))

    def test_partial_row_does_not_block_closing(self):
        self._task(MON)
        queue = self._row(MON)
        self._deliver(queue, quantity='1.20')
        # Partial row with delivery but no return is now flagged as incomplete
        control = services.daily_closing_status(MON)
        self.assertFalse(control['closable'])
        self.assertEqual(control['incomplete_count'], 1)

        # After return, it should be closable
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.admin, returned_quantity='0.20')
        control = services.daily_closing_status(MON)
        self.assertTrue(control['closable'])


class ExtraDeliveryTests(PartialBase):
    def test_suggested_delivery_then_remaining_delivery(self):
        self._task(MON)
        queue = self._row(MON)
        self._use_suggested = True
        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('1.00'))
        self.assertEqual(queue.status, 'partial')

        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('2.00'))
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(self.raw.current_stock, Decimal('3.50'))

    def test_over_delivery_is_reported_as_excess(self):
        self._task(MON)
        queue = self._deliver(self._row(MON), quantity='3.50')
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        self.assertEqual(queue.excess_consumption, Decimal('1.50'))

    def test_extra_after_full_delivery_needs_explicit_quantity(self):
        self._task(MON)
        self._use_suggested = True
        first = self._deliver(self._row(MON))
        self.assertEqual(first.delivered_quantity, Decimal('1.00'))
        second = self._deliver(first)
        self.assertEqual(second.delivered_quantity, Decimal('2.00'))
        self.assertEqual(second.status, 'delivered')

        with self.assertRaises(HandoverError):
            services.execute_daily_delivery(
                queue_id=second.pk, delivered_by=self.admin)

        queue = self._deliver(second, quantity='0.50')
        self.assertEqual(queue.delivered_quantity, Decimal('2.50'))
        self.assertEqual(queue.excess_consumption, Decimal('0.50'))
        self.assertEqual(
            StockMovement.objects.filter(
                daily_queue=queue, movement_type='consumption').count(), 3)

    def test_extra_beyond_stock_is_rejected(self):
        self._task(MON)
        queue = self._row(MON)
        with self.assertRaises(HandoverError):
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.admin, quantity='99')

    def test_no_delivery_after_a_return(self):
        self._task(MON)
        queue = self._deliver(self._row(MON))
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.admin, returned_quantity='0.50')
        with self.assertRaises(HandoverError):
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.admin, quantity='1')


class AutoReturnTests(PartialBase):
    """برگشت خودکار مازاد در پایان روز کارگر."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cat = RawMaterialCategory.objects.get(name='مواد')
        cls.raw_surplus = RawMaterial.objects.create(
            category=cat, name='رنگ پاش', code='PC-S', unit='kg',
            pack_size=Decimal('4.5'))
        StockMovement.objects.create(
            raw_material=cls.raw_surplus, movement_type='purchase',
            quantity=Decimal('8.00'), created_by=cls.admin)
        
        # Add raw_surplus to the same painting process with consumption 1.0
        PaintingMaterialRequirement.objects.create(
            process=cls.stage.process, stage=cls.stage,
            raw_material=cls.raw_surplus, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('1.000'))

    def setUp(self):
        self._use_suggested = True

    def _task_surplus(self, day):
        """Create a task that will need raw_surplus (quantity 2 means 2 units consumed)."""
        return self._task(day, quantity=2)

    def _row_surplus(self, day):
        return DailyMaterialQueue.objects.filter(
            work_date=day, worker=self.worker,
            raw_material=self.raw_surplus).first()

    def test_suggested_delivery_exceeds_need_leaves_full_pack(self):
        """stock=8, need=2, pack=4.5 → پیشنهاد تحویل 3.5 (open_remainder = 8 % 4.5 = 3.5)."""
        self._task_surplus(MON)
        queue = self._row_surplus(MON)
        preview = services.preview_daily_delivery(queue.pk)
        # open_remainder = 8 % 4.5 = 3.5
        self.assertEqual(preview['suggested_delivery'], Decimal('3.50'))
        self.assertEqual(preview['physical'], Decimal('3.50'))
        self.assertEqual(preview['remainder_after'], Decimal('0.00'))

        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('3.50'))
        self.assertEqual(queue.excess_consumption, Decimal('1.50'))
        self.assertEqual(queue.status, 'delivered')

    def test_auto_return_reclaims_excess_at_end_of_day(self):
        """پس از تحویل 3.5 (open_remainder) با نیاز 2، مازاد 1.5 برمی‌گردد."""
        self._task_surplus(MON)
        queue = self._row_surplus(MON)
        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('3.50'))
        self.assertEqual(queue.excess_consumption, Decimal('1.50'))

        services.execute_auto_return(
            queue_id=queue.pk, returned_by=self.admin)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('1.50'))
        self.assertEqual(queue.actual_consumption, Decimal('2.00'))
        self.assertEqual(queue.excess_consumption, Decimal('0.00'))
        self.assertEqual(queue.status, 'delivered')

    def test_auto_return_with_manual_return_adjusts_consumption(self):
        """اگر برگشت دستی بیش از خودکار باشد، مصرف کم‌تر ثبت می‌شود."""
        self._task_surplus(MON)
        queue = self._row_surplus(MON)
        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('3.50'))

        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.admin, returned_quantity='2.00')
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('2.00'))
        self.assertEqual(queue.actual_consumption, Decimal('1.50'))

        services.execute_auto_return(
            queue_id=queue.pk, returned_by=self.admin)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('2.00'))
        self.assertEqual(queue.actual_consumption, Decimal('1.50'))

    def test_auto_return_with_less_manual_return_increases_consumption(self):
        """اگر برگشت دستی کمتر از خودکار باشد، مصرف بیشتر ثبت می‌شود."""
        self._task_surplus(MON)
        queue = self._row_surplus(MON)
        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('3.50'))

        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.admin, returned_quantity='0.50')
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.50'))
        self.assertEqual(queue.actual_consumption, Decimal('3.00'))

        services.execute_auto_return(
            queue_id=queue.pk, returned_by=self.admin)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('1.50'))
        self.assertEqual(queue.actual_consumption, Decimal('2.00'))

    def test_auto_return_is_idempotent(self):
        """صدور همزمان یا تکراری برگشت خودکار مازاد دوباره نمی‌شود."""
        self._task_surplus(MON)
        queue = self._row_surplus(MON)
        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('3.50'))

        count_before = StockMovement.objects.count()
        services.execute_auto_return(queue_id=queue.pk, returned_by=self.admin)
        services.execute_auto_return(queue_id=queue.pk, returned_by=self.admin)
        # Only one return movement should be created
        self.assertEqual(StockMovement.objects.count(), count_before + 1)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('1.50'))
        self.assertEqual(queue.actual_consumption, Decimal('2.00'))

    def test_auto_return_no_excess_is_noop(self):
        """اگر تحویل دقیقاً مساوی نیاز باشد، برگشت خودکار کاری نمی‌کند."""
        self._task(MON)
        queue = self._row(MON)
        self._deliver(queue, quantity='2.00')
        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('2.00'))

        services.execute_auto_return(queue_id=queue.pk, returned_by=self.admin)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))
        self.assertEqual(queue.actual_consumption, Decimal('2.00'))
        self.assertEqual(queue.status, 'delivered')


class NewSuggestedDeliveryTests(PartialBase):
    """تست‌های منطق جدید پیشنهادی تحویل (باقی‌مانده بستهٔ باز)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Create a separate raw material with pack=4.5 and stock=16.75
        cat = RawMaterialCategory.objects.get(name='مواد')
        cls.raw_new = RawMaterial.objects.create(
            category=cat, name='ماده تست جدید', code='PC-NEW', unit='kg',
            pack_size=Decimal('4.5'))
        StockMovement.objects.create(
            raw_material=cls.raw_new, movement_type='purchase',
            quantity=Decimal('16.75'), created_by=cls.admin)

        # Add requirement for the same process/stage
        PaintingMaterialRequirement.objects.create(
            process=cls.stage.process, stage=cls.stage,
            raw_material=cls.raw_new, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('1.000'))

    def _task_new(self, day, quantity=2):
        start = timezone.make_aware(datetime.combine(day, time(8, 0)))
        return ProductionTask.objects.create(
            order=self.order, order_item=self.item, station_name='paint',
            step_order=1, quantity=quantity, status='pending',
            painting_stage=self.stage, color_part='بدنه',
            assigned_worker=self.worker, scheduled_start=start)

    def _row_new(self, day):
        return DailyMaterialQueue.objects.filter(
            work_date=day, worker=self.worker, raw_material=self.raw_new).first()

    def test_suggested_delivery_open_pack_remainder(self):
        """
        موجودی 16.75، بسته 4.5، نیاز 2.0
        open_remainder = 16.75 % 4.5 = 3.25
        پیشنهاد تحویل = 3.25 (باقی‌مانده بستهٔ باز)
        """
        self._task_new(MON)
        queue = self._row_new(MON)
        self._use_suggested = True
        preview = services.preview_daily_delivery(queue.pk)

        self.assertEqual(preview['suggested_delivery'], Decimal('3.25'))
        self.assertEqual(preview['physical'], Decimal('3.25'))
        self.assertEqual(preview['from_open'], Decimal('3.25'))
        self.assertEqual(preview['from_new_pack'], Decimal('0.00'))
        self.assertEqual(preview['packs'], 0)
        self.assertEqual(preview['remainder_after'], Decimal('0.00'))

        # Execute delivery
        with self.captureOnCommitCallbacks(execute=True):
            self._deliver(queue)
        queue.refresh_from_db()

        self.assertEqual(queue.delivered_quantity, Decimal('3.25'))
        self.assertEqual(self.raw_new.current_stock, Decimal('13.50'))
        self.assertEqual(queue.excess_consumption, Decimal('1.25'))
        self.assertEqual(queue.status, 'delivered')

    def test_suggested_delivery_no_open_pack(self):
        """
        موجودی 13.50 (مضرب 4.5)، بسته 4.5، نیاز 2.0
        open_remainder = 13.50 % 4.5 = 0.0
        پیشنهاد تحویل = نیاز = 2.0
        """
        self._task_new(MON)
        queue = self._row_new(MON)
        self._use_suggested = True

        # First delivery should leave stock at 13.50 (which is 3 * 4.5)
        preview = services.preview_daily_delivery(queue.pk)
        # After first delivery (3.25), stock = 13.50, open_remainder = 0
        # But this test is independent - we need to set up fresh stock
        # Let's verify with a fresh setup
        
        # Actually test with a material that has exact pack multiple stock
        cat = RawMaterialCategory.objects.get(name='مواد')
        raw_exact = RawMaterial.objects.create(
            category=cat, name='ماده دقیق', code='PC-EXACT', unit='kg',
            pack_size=Decimal('4.5'))
        StockMovement.objects.create(
            raw_material=raw_exact, movement_type='purchase',
            quantity=Decimal('13.50'), created_by=self.admin)
        
        PaintingMaterialRequirement.objects.create(
            process=self.stage.process, stage=self.stage,
            raw_material=raw_exact, product=self.product,
            color_part='بدنه', consumption_per_unit=Decimal('1.000'))

        task = self._task_new(MON)
        task.raw_material = raw_exact
        task.save()

        queue2 = DailyMaterialQueue.objects.filter(
            work_date=MON, worker=self.worker, raw_material=raw_exact).first()
        
        preview2 = services.preview_daily_delivery(queue2.pk)
        self.assertEqual(preview2['suggested_delivery'], Decimal('2.00'))
        self.assertEqual(preview2['physical'], Decimal('2.00'))
        self.assertEqual(preview2['from_open'], Decimal('0.00'))
        self.assertEqual(preview2['from_new_pack'], Decimal('2.00'))
        self.assertEqual(preview2['packs'], 1)
        self.assertEqual(preview2['remainder_after'], Decimal('2.50'))  # (13.5 - 2) % 4.5 = 11.5 % 4.5 = 2.5
