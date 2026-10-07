"""
Phase 3 — اتصال واقعی صف مواد روزانه به برنامهٔ Painting Schedule.

زنجیرهٔ تحت آزمون:
    ProductionTask → PaintingStage → PaintingMaterialRequirement → DailyMaterialQueue
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from inventory import services
from inventory.models import (
    DailyMaterialQueue,
    DailyMaterialQueueSource,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
)
from product.models import (
    Color,
    Order,
    OrderItem,
    PaintingMaterialRequirement,
    PaintingProcess,
    PaintingStage,
    Product,
    ProductCategory,
    ProductionTask,
    Customer,
)


class DailyQueuePhase3Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_superuser('q3admin', password='pw')
        cls.worker_a = User.objects.create_user('worker_a', password='pw')
        cls.worker_b = User.objects.create_user('worker_b', password='pw')

        cls.raw_category = RawMaterialCategory.objects.create(name='مواد نقاشی')
        cls.raw = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ سفید', code='R-WHITE',
            unit='kg', pack_size=Decimal('0'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته تست')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول تست', base_price=1000,
        )

        cls.process = PaintingProcess.objects.create(
            name='روند نقاشی', code='P3', color_codes=['8'], is_active=True,
        )
        cls.stage_1 = PaintingStage.objects.create(
            process=cls.process, order=1, name='زیرکار',
            duration_minutes=30, drying_time_minutes=0,
        )
        cls.stage_2 = PaintingStage.objects.create(
            process=cls.process, order=2, name='رنگ نهایی',
            duration_minutes=30, drying_time_minutes=0,
        )

        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.manager, customer=cls.customer, number='Q3-1',
        )
        cls.order_item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=2,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)

        # مصرف به ازای هر واحد محصول: 0.5 کیلوگرم
        cls.requirement = PaintingMaterialRequirement.objects.create(
            process=cls.process, stage=cls.stage_1, raw_material=cls.raw, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.500'),
        )
        cls.requirement_2 = PaintingMaterialRequirement.objects.create(
            process=cls.process, stage=cls.stage_2, raw_material=cls.raw, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.500'),
        )

        # یک OrderItem دوم برای تست تجمیع دو تسک مستقل
        cls.order_item_2 = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=3,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item_2)

        # موجودی اولیه
        StockMovement.objects.create(
            raw_material=cls.raw, movement_type='purchase',
            quantity=Decimal('100'), created_by=cls.manager,
        )

        cls.day1 = datetime(2026, 3, 2, 8, 0)      # دوشنبه
        cls.day2 = datetime(2026, 3, 3, 8, 0)
        cls.day3 = datetime(2026, 3, 4, 8, 0)

    def _at(self, moment):
        return timezone.make_aware(
            datetime.combine(moment.date(), time(moment.hour, moment.minute)),
        )

    def _date_of(self, moment):
        return moment.date()

    def _make_task(self, *, stage=None, worker=None, start=None, quantity=2,
                   order_item=None):
        return ProductionTask.objects.create(
            order=self.order,
            order_item=order_item or self.order_item,
            station_name='paint',
            step_order=1,
            quantity=quantity,
            status='pending',
            painting_stage=stage or self.stage_1,
            color_part='بدنه',
            assigned_worker=worker,
            scheduled_start=self._at(start or self.day1) if start else None,
            scheduled_end=None,
        )

    def _queue(self, work_date, worker, raw=None):
        return DailyMaterialQueue.objects.filter(
            work_date=work_date, worker=worker, raw_material=raw or self.raw,
        ).first()


class QueueBuildTests(DailyQueuePhase3Base):
    def test_01_task_creation_builds_queue(self):
        """۱. ایجاد Task → Queue ایجاد شود."""
        self._make_task(worker=self.worker_a, start=self.day1, quantity=2)

        queue = self._queue(self._date_of(self.day1), self.worker_a)
        self.assertIsNotNone(queue)
        # 2 واحد × 0.5 = 1
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))
        self.assertEqual(queue.status, 'pending')

        sources = list(queue.sources.all())
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].painting_stage_id, self.stage_1.id)

    def test_02_date_change_moves_queue(self):
        """۲. تغییر تاریخ → Queue از روز قبلی به روز جدید منتقل شود."""
        task = self._make_task(worker=self.worker_a, start=self.day1)
        day1_date = self._date_of(self.day1)
        day2_date = self._date_of(self.day2)
        self.assertIsNotNone(self._queue(day1_date, self.worker_a))

        task.scheduled_start = self._at(self.day2)
        task.save()

        old_queue = self._queue(day1_date, self.worker_a)
        self.assertEqual(old_queue.status, 'cancelled')

        new_queue = self._queue(day2_date, self.worker_a)
        self.assertIsNotNone(new_queue)
        self.assertEqual(new_queue.status, 'pending')
        self.assertEqual(new_queue.planned_quantity, Decimal('1.00'))
        self.assertEqual(new_queue.sources.count(), 1)

    def test_03_worker_change_moves_queue(self):
        """۳. تغییر Worker → صف کارگر قبلی و جدید اصلاح شود."""
        task = self._make_task(worker=self.worker_a, start=self.day1)
        self.assertIsNotNone(self._queue(self._date_of(self.day1), self.worker_a))

        task.assigned_worker = self.worker_b
        task.save()

        old_queue = self._queue(self._date_of(self.day1), self.worker_a)
        new_queue = self._queue(self._date_of(self.day1), self.worker_b)
        self.assertEqual(old_queue.status, 'cancelled')
        self.assertIsNotNone(new_queue)
        self.assertEqual(new_queue.planned_quantity, Decimal('1.00'))

    def test_04_quantity_change_updates_planned(self):
        """۴. تغییر Quantity → planned quantity اصلاح شود."""
        task = self._make_task(worker=self.worker_a, start=self.day1, quantity=2)
        queue = self._queue(self._date_of(self.day1), self.worker_a)
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))

        task.quantity = 4
        task.save()

        queue.refresh_from_db()
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        self.assertEqual(queue.sources.get().quantity, Decimal('2.00'))

    def test_04b_partial_completion_does_not_reduce_planned(self):
        """Decision 10: وضعیت انجام کار نباید planned quantity را کم کند."""
        task = self._make_task(worker=self.worker_a, start=self.day1, quantity=4)
        task.completed_quantity = 1
        task.save()

        queue = self._queue(self._date_of(self.day1), self.worker_a)
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        self.assertEqual(queue.status, 'pending')

    def test_05_delete_task_removes_need(self):
        """۵. حذف Task → نیاز فعال حذف شود."""
        task = self._make_task(worker=self.worker_a, start=self.day1)
        queue = self._queue(self._date_of(self.day1), self.worker_a)
        self.assertIsNotNone(queue)

        task.delete()

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'cancelled')
        self.assertEqual(queue.sources.count(), 0)

    def test_05b_unscheduling_task_removes_need(self):
        task = self._make_task(worker=self.worker_a, start=self.day1)
        queue = self._queue(self._date_of(self.day1), self.worker_a)
        self.assertIsNotNone(queue)

        ProductionTask.objects.filter(pk=task.pk).update(
            scheduled_start=None, scheduled_end=None, assigned_worker=None,
        )

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'cancelled')

    def test_06_two_tasks_are_aggregated(self):
        """۶. دو Task یک worker/material/date → Queue تجمیعی."""
        self._make_task(worker=self.worker_a, start=self.day1, quantity=2)
        self._make_task(
            worker=self.worker_a, start=self.day1, quantity=3,
            order_item=self.order_item_2,
        )

        rows = DailyMaterialQueue.objects.filter(
            work_date=self._date_of(self.day1), worker=self.worker_a,
            raw_material=self.raw,
        )
        self.assertEqual(rows.count(), 1)
        queue = rows.get()
        # 2×0.5 + 3×0.5 = 2.5
        self.assertEqual(queue.planned_quantity, Decimal('2.50'))
        self.assertEqual(queue.sources.count(), 2)

    def test_07_repeated_sync_is_idempotent(self):
        """۷. اجرای چندباره Sync → duplicate ایجاد نشود."""
        self._make_task(worker=self.worker_a, start=self.day1, quantity=2)
        self._make_task(
            worker=self.worker_a, start=self.day1, quantity=3,
            order_item=self.order_item_2,
        )
        date = self._date_of(self.day1)

        for _ in range(3):
            services.sync_daily_material_queue(date)

        self.assertEqual(
            DailyMaterialQueue.objects.filter(
                work_date=date, worker=self.worker_a, raw_material=self.raw,
            ).count(),
            1,
        )
        queue = self._queue(date, self.worker_a)
        self.assertEqual(queue.planned_quantity, Decimal('2.50'))
        self.assertEqual(queue.sources.count(), 2)
        self.assertEqual(
            DailyMaterialQueueSource.objects.filter(queue=queue).count(), 2,
        )

    def test_10_task_without_worker_creates_no_queue(self):
        """۱۰. Task بدون assigned_worker → Queue تحویل ایجاد نشود."""
        self._make_task(worker=None, start=self.day1, quantity=2)

        self.assertFalse(
            DailyMaterialQueue.objects.filter(
                work_date=self._date_of(self.day1), raw_material=self.raw,
            ).exists()
        )

    def test_10b_task_without_date_or_stage_creates_no_queue(self):
        self._make_task(worker=self.worker_a, start=None)
        self.assertFalse(
            DailyMaterialQueue.objects.filter(raw_material=self.raw).exists()
        )

        task = self._make_task(worker=self.worker_a, start=self.day1)
        self.assertEqual(
            DailyMaterialQueue.objects.filter(
                raw_material=self.raw, status='pending',
            ).count(),
            1,
        )

        # بدون مرحله نقاشی، نیاز فعالی باقی نمی‌ماند
        task.painting_stage = None
        task.save()
        self.assertEqual(
            DailyMaterialQueue.objects.filter(
                raw_material=self.raw, status='pending',
            ).count(),
            0,
        )


class QueueTransactionGuardTests(DailyQueuePhase3Base):
    def test_08_delivered_row_keeps_history(self):
        """۸. Queue دارای delivery → delivery history دست‌نخورده بماند."""
        task = self._make_task(worker=self.worker_a, start=self.day1, quantity=2)
        queue = self._queue(self._date_of(self.day1), self.worker_a)
        services.execute_daily_delivery(queue_id=queue.id, delivered_by=self.manager)

        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('1.00'))
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(
            queue.actual_consumption, Decimal('1.00'),
        )

        # تغییر برنامه: مقدار تولید بیشتر می‌شود
        task.quantity = 6
        task.save()

        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('1.00'))
        self.assertEqual(queue.status, 'delivered')
        self.assertTrue(queue.has_plan_conflict)
        self.assertTrue(queue.conflict_note)
        # planned_quantity و منابع دست‌نخورده
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))
        self.assertEqual(queue.sources.count(), 1)
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw, movement_type='consumption',
            ).count(),
            1,
        )

    def test_09_returned_row_keeps_history(self):
        """۹. Queue دارای return → return history دست‌نخورده بماند."""
        task = self._make_task(worker=self.worker_a, start=self.day1, quantity=4)
        queue = self._queue(self._date_of(self.day1), self.worker_a)
        services.execute_daily_delivery(queue_id=queue.id, delivered_by=self.manager)
        services.execute_daily_return(
            queue_id=queue.id, returned_by=self.manager,
            returned_quantity=Decimal('0.50'),
        )

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.50'))
        self.assertEqual(queue.delivered_quantity, Decimal('2.00'))
        self.assertEqual(queue.actual_consumption, Decimal('1.50'))
        movements_before = list(
            StockMovement.objects.filter(raw_material=self.raw)
            .values_list('movement_type', 'quantity')
        )

        # برنامه تغییر می‌کند
        task.quantity = 1
        task.save()

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.50'))
        self.assertEqual(queue.delivered_quantity, Decimal('2.00'))
        self.assertTrue(queue.has_plan_conflict)
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        self.assertEqual(
            list(
                StockMovement.objects.filter(raw_material=self.raw)
                .values_list('movement_type', 'quantity')
            ),
            movements_before,
        )

    def test_09b_returned_material_becomes_general_stock(self):
        """Decision 7: برگشتی عمومی است و به کارگر تعلق ندارد."""
        task = self._make_task(worker=self.worker_a, start=self.day1, quantity=2)
        queue = self._queue(self._date_of(self.day1), self.worker_a)
        stock_before = self.raw.current_stock

        services.execute_daily_delivery(queue_id=queue.id, delivered_by=self.manager)
        stock_after_delivery = self.raw.current_stock
        services.execute_daily_return(
            queue_id=queue.id, returned_by=self.manager,
            returned_quantity=Decimal('1.00'),
        )
        stock_after_return = self.raw.current_stock

        self.assertEqual(stock_after_delivery, stock_before - Decimal('1.00'))
        self.assertEqual(stock_after_return, stock_before)
        # در Engine B بازگشت فقط موجودی انبار را برمی‌گرداند و هیچ «امانت
        # نزد کارگر» ساخته نمی‌شود؛ همان صف روزانه تنها محل نگهداری نیاز است.
        self.assertFalse(
            self.raw.daily_queues.filter(delivered_quantity=Decimal('0')).exists(),
            'بازگشت نباید ردیف صف تازه‌ای بسازد.',
        )
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('1.00'))

    def test_delivered_row_dropped_from_plan_is_flagged_not_removed(self):
        task = self._make_task(worker=self.worker_a, start=self.day1, quantity=2)
        queue = self._queue(self._date_of(self.day1), self.worker_a)
        services.execute_daily_delivery(queue_id=queue.id, delivered_by=self.manager)

        task.delete()

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(queue.delivered_quantity, Decimal('1.00'))
        self.assertTrue(queue.has_plan_conflict)

    def test_cancelled_row_is_revived_when_need_returns(self):
        task = self._make_task(worker=self.worker_a, start=self.day1)
        queue = self._queue(self._date_of(self.day1), self.worker_a)

        task.scheduled_start = None
        task.save()
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'cancelled')

        task.scheduled_start = self._at(self.day1)
        task.save()
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))


class QueueMultiDateTests(DailyQueuePhase3Base):
    def test_date_move_is_atomic_across_days(self):
        """Decision 5: انتقال بین دو روز، هر دو صف را با هم اصلاح می‌کند."""
        task = self._make_task(worker=self.worker_a, start=self.day1)
        day1_date, day2_date = self._date_of(self.day1), self._date_of(self.day2)

        rows = services.sync_daily_material_queue_for_dates([day1_date, day2_date])
        self.assertTrue(rows)

        task.scheduled_start = self._at(self.day2)
        task.save()

        self.assertEqual(
            self._queue(day1_date, self.worker_a).status, 'cancelled',
        )
        self.assertEqual(
            self._queue(day2_date, self.worker_a).status, 'pending',
        )
        self.assertEqual(
            DailyMaterialQueue.objects.filter(
                work_date=day1_date, status='pending',
            ).count(),
            0,
        )

    def test_sync_scope_batches_requests(self):
        """همهٔ درخواست‌های داخل یک scope، یک بار در پایان اعمال می‌شوند."""
        from inventory.services import _pending_sync_dates

        self._make_task(worker=self.worker_a, start=self.day1)
        with services.queue_sync_scope() as pending:
            services.sync_queue_for_date(self._date_of(self.day1))
            services.sync_queue_for_date(self._date_of(self.day2))
            self.assertEqual(pending, {self._date_of(self.day1), self._date_of(self.day2)})
            self.assertIsNotNone(_pending_sync_dates())

        queue = self._queue(self._date_of(self.day1), self.worker_a)
        self.assertIsNotNone(queue)
        self.assertIsNone(_pending_sync_dates())


class MultiStageReportTests(DailyQueuePhase3Base):
    def test_multistage_overlap_is_reported_only(self):
        """
        Decision 2: مصرف در سطح Process تعریف شده اما Stage چندگانه است.
        مدل جدید ساخته نمی‌شود؛ فقط گزارش داده می‌شود.
        """
        # Same order_item going through multiple stages = multistage overlap
        self._make_task(worker=self.worker_a, start=self.day1, stage=self.stage_1)
        self._make_task(
            worker=self.worker_a, start=self.day1, stage=self.stage_2,
            order_item=self.order_item, quantity=3,
        )

        overlaps = services.detect_multistage_overlaps(self._date_of(self.day1))
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]['process_id'], self.process.id)
        self.assertEqual(
            sorted(overlaps[0]['stage_ids']),
            sorted([self.stage_1.id, self.stage_2.id]),
        )

        # Now each stage creates its own queue row
        queues = DailyMaterialQueue.objects.filter(
            work_date=self._date_of(self.day1), worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(queues.count(), 2)
        for q in queues:
            self.assertEqual(q.sources.count(), 1)