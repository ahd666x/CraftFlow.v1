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

        self._deliver(carried)
        self.assertEqual(carried.delivered_quantity, Decimal('0.80'))
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
        self.assertTrue(services.daily_closing_status(MON)['closable'])


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
        """stock=8, need=2, pack=4.5 → پیشنهاد تحویل 3.5 (= 8 - 4.5)."""
        self._task_surplus(MON)
        queue = self._row_surplus(MON)
        preview = services.preview_daily_delivery(queue.pk)
        self.assertEqual(preview['suggested_delivery'], Decimal('3.50'))
        self.assertEqual(preview['physical'], Decimal('3.50'))
        self.assertEqual(preview['remainder_after'], Decimal('0.00'))

        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('3.50'))
        self.assertEqual(queue.excess_consumption, Decimal('1.50'))
        self.assertEqual(queue.status, 'delivered')

    def test_auto_return_reclaims_excess_at_end_of_day(self):
        """پس از تحویل 3.5 با نیاز 2، برگشت خودکار 1.5 ثبت می‌شود."""
        self._task_surplus(MON)
        queue = self._row_surplus(MON)
        self._deliver(queue)
        self.assertEqual(queue.delivered_quantity, Decimal('3.50'))

        services.execute_auto_return(
            queue_id=queue.pk, returned_by=self.admin)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('1.50'))
        self.assertEqual(queue.actual_consumption, Decimal('2.00'))
        self.assertEqual(queue.excess_consumption, Decimal('0.00'))
        # مصرف دقیقاً معادل نیاز است؛ ردیف در وضعیت «تحویل شده» باقی می‌ماند
        self.assertEqual(queue.status, 'delivered')

    def test_auto_return_with_manual_return_adjusts_consumption(self):
        """اگر برگشت دستی بیش از خودکار باشد، مصرف کم‌تر ثبت می‌شود."""
        self._task_surplus(MON)
        queue = self._row_surplus(MON)
        self._deliver(queue)

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

        count_before = StockMovement.objects.count()
        services.execute_auto_return(queue_id=queue.pk, returned_by=self.admin)
        services.execute_auto_return(queue_id=queue.pk, returned_by=self.admin)
        self.assertEqual(StockMovement.objects.count(), count_before + 1)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('1.50'))

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
