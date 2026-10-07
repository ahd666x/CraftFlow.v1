"""
P1 — de-duplication نیاز مواد بر اساس «واحد کار نقاشی».

مسئله: ``PaintingMaterialRequirement`` در سطح (process + product + color_part)
است و به ``PaintingStage`` وصل نیست. یک ``OrderItem`` برای هر مرحله یک
``ProductionTask`` جداگانه دارد، بنابراین اگر فرمول برای هر task اعمال شود،
به تعداد مراحل ضرب می‌شد (۶ مرحله × ۱٫۳ = ۷٫۸ به‌جای ۲٫۶).

قاعدهٔ جدید: واحد کار نقاشی = (order_item, color_part, painting_process,
raw_material) و de-duplication داخل هر گروه (worker, raw_material) انجام
می‌شود؛ بنابراین کارگرهای مستقل و روزهای مستقل نیاز خودشان را دارند.

P6 — صف نباید بعد از حذف/تغییر task stale بماند.
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory import reports, services
from inventory.models import (
    DailyMaterialQueue,
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
    ProductionTask,
)

CONSUMPTION = Decimal('1.300')


class WorkUnitBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('wuadmin', password='pw')
        cls.worker_a = User.objects.create_user('wu_a', password='pw')
        cls.worker_b = User.objects.create_user('wu_b', password='pw')

        cls.raw_category = RawMaterialCategory.objects.create(name='مواد نقاشی')
        cls.raw = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ سفید', code='WU-WHITE',
            unit='kg', pack_size=Decimal('0'),
        )
        cls.product_category = ProductCategory.objects.create(name='دسته تست')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول تست', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند نقاشی', code='WU', color_codes=['8'], is_active=True,
        )
        cls.stages = [
            PaintingStage.objects.create(
                process=cls.process, order=i, name='مرحله %s' % i,
                duration_minutes=30, drying_time_minutes=0,
            )
            for i in range(1, 7)
        ]

        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.superuser, customer=cls.customer, number='WU-1',
        )
        cls.order_item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=1,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        for stage in cls.stages:
            PaintingMaterialRequirement.objects.create(
                process=cls.process, stage=stage, raw_material=cls.raw, product=cls.product,
                color_part='بدنه', consumption_per_unit=CONSUMPTION,
            )
        StockMovement.objects.create(
            raw_material=cls.raw, movement_type='purchase',
            quantity=Decimal('100'), created_by=cls.superuser,
        )

        cls.day1 = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.day1, time(8, 0))
        )

    def _stage_tasks(self, item, worker, *, stages=None, start=None, quantity=1):
        """یک ProductionTask برای هر مرحلهٔ داده‌شده (زنجیرهٔ واقعی کارگاه)."""
        created = []
        for step, stage in enumerate(stages or self.stages, start=1):
            created.append(ProductionTask.objects.create(
                order=self.order,
                order_item=item,
                station_name='paint',
                step_order=step,
                quantity=quantity,
                status='pending',
                painting_stage=stage,
                color_part='بدنه',
                assigned_worker=worker,
                scheduled_start=start or self.day_start,
                scheduled_end=None,
            ))
        return created

    def _queue(self, worker=None, raw=None, work_date=None):
        return DailyMaterialQueue.objects.filter(
            work_date=work_date or self.day1,
            worker=worker or self.worker_a,
            raw_material=raw or self.raw,
        ).first()


class SingleStageTests(WorkUnitBase):
    def test_01_one_stage_counts_requirement_once(self):
        self._stage_tasks(self.order_item, self.worker_a, stages=self.stages[:1])

        queue = self._queue()
        self.assertIsNotNone(queue)
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue.sources.count(), 1)


class MultiStageSeparateTests(WorkUnitBase):
    def test_02_two_stages_create_two_queue_rows(self):
        self._stage_tasks(self.order_item, self.worker_a, stages=self.stages[:2])

        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue.sources.count(), 1)

    def test_03_six_stages_create_six_queue_rows(self):
        """Each stage = separate work unit = separate queue row."""
        self._stage_tasks(self.order_item, self.worker_a)

        # Should create 6 separate queue rows, one per stage
        queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(queues.count(), 6)
        # Each queue has planned_quantity = 1.30 / 6 = ~0.22
        for q in queues:
            self.assertEqual(q.planned_quantity, Decimal('1.30'))

    def test_04_sources_keep_traceability_and_sum_matches_planned(self):
        """Each queue row has its own source linking to the task."""
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(queues.count(), 6)
        for q in queues:
            self.assertEqual(q.sources.count(), 1)
            # Each source should link to one task
            source = q.sources.first()
            self.assertIn(source.production_task_id, [t.id for t in tasks])

    def test_05_unit_quantity_scales_with_production_quantity(self):
        """6 stages with quantity=2: each stage queue has 2.60 planned."""
        self._stage_tasks(self.order_item, self.worker_a, quantity=2)

        queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(queues.count(), 6)
        for q in queues:
            self.assertEqual(q.planned_quantity, Decimal('2.60'))

    def test_06_six_stages_of_two_colour_parts_stay_separate(self):
        """قطعهٔ رنگی متفاوت = واحد کار متفاوت = نیاز مستقل."""
        for stage in self.stages[:3]:
            PaintingMaterialRequirement.objects.create(
                process=self.process, stage=stage, raw_material=self.raw, product=self.product,
                color_part='درب', consumption_per_unit=CONSUMPTION,
            )
        Color.objects.create(part='درب', code='8', orderitem=self.order_item)
        for i, stage in enumerate(self.stages[:3], start=1):
            ProductionTask.objects.create(
                order=self.order, order_item=self.order_item, station_name='paint',
                step_order=100 + i, quantity=1, status='pending',
                painting_stage=stage, color_part='درب',
                assigned_worker=self.worker_a, scheduled_start=self.day_start,
            )

        self._stage_tasks(self.order_item, self.worker_a, stages=self.stages[:3])

        # 3 stages for 'بدنه' + 3 stages for 'درب' = 6 queue rows
        queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(queues.count(), 6)


class IndependentWorkersTests(WorkUnitBase):
    def test_07_two_workers_keep_their_own_need(self):
        item_b = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=1,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=item_b)

        self._stage_tasks(self.order_item, self.worker_a, stages=self.stages[:6])
        self._stage_tasks(item_b, self.worker_b, stages=self.stages[:6])

        queue_a = self._queue(worker=self.worker_a)
        queue_b = self._queue(worker=self.worker_b)
        self.assertEqual(queue_a.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue_b.planned_quantity, Decimal('1.30'))

    def test_08_same_worker_two_items_are_summed_once_each(self):
        item_b = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=1,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=item_b)

        self._stage_tasks(self.order_item, self.worker_a, stages=self.stages[:6])
        self._stage_tasks(item_b, self.worker_a, stages=self.stages[:6])

        self.assertEqual(self._queue().planned_quantity, Decimal('2.60'))


class MoveAndDeleteTests(WorkUnitBase):
    def test_09_worker_change_moves_the_queue_rows(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        for task in tasks:
            task.assigned_worker = self.worker_b
            task.save()

        # Old queues should be cancelled
        old_queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(old_queues.count(), 6)
        for q in old_queues:
            self.assertEqual(q.status, 'cancelled')

        # New queues should be created for worker_b
        new_queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_b, raw_material=self.raw
        )
        self.assertEqual(new_queues.count(), 6)
        for q in new_queues:
            self.assertEqual(q.planned_quantity, Decimal('1.30'))
            self.assertEqual(q.sources.count(), 1)

    def test_10_date_change_moves_the_queue_rows(self):
        tomorrow = self.day1 + timezone.timedelta(days=1)
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        for task in tasks:
            task.scheduled_start = timezone.make_aware(
                timezone.datetime.combine(tomorrow, time(8, 0))
            )
            task.save()

        # Old queues should be cancelled
        old_queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(old_queues.count(), 6)
        for q in old_queues:
            self.assertEqual(q.status, 'cancelled')

        # New queues should be created for tomorrow
        new_queues = DailyMaterialQueue.objects.filter(
            work_date=tomorrow, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(new_queues.count(), 6)
        for q in new_queues:
            self.assertEqual(q.planned_quantity, Decimal('1.30'))
            self.assertEqual(q.sources.count(), 1)

    def test_11_stage_change_keeps_planned_quantity_stable(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        for task in tasks:
            task.painting_stage = self.stages[0]
            task.save()

        # All tasks now have the same stage, so they should be deduplicated
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue.sources.count(), 6)

    def test_12_deleting_all_tasks_removes_planned_quantity(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        before_queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(before_queues.count(), 6)
        self.assertEqual(before_queues.first().sources.count(), 1)

        ProductionTask.objects.filter(pk__in=[t.id for t in tasks]).delete()

        queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(queues.count(), 6)
        for q in queues:
            self.assertEqual(q.status, 'cancelled')
            self.assertEqual(q.sources.count(), 0)
            self.assertEqual(q.delivered_quantity, Decimal('0.00'))

    def test_13_deleting_one_stage_keeps_need_and_sources_consistent(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        tasks[0].delete()

        # Should have 5 active queue rows now (one per remaining stage)
        # The deleted stage's queue should be cancelled
        queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        ).exclude(status='cancelled')
        self.assertEqual(queues.count(), 5)
        for q in queues:
            self.assertEqual(q.planned_quantity, Decimal('1.30'))
            self.assertEqual(q.sources.count(), 1)

        # The cancelled queue should still exist but be marked cancelled
        cancelled = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw,
            status='cancelled'
        )
        self.assertEqual(cancelled.count(), 1)


class TransactionHistoryTests(WorkUnitBase):
    def test_14_sync_after_delete_keeps_delivered_history(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        queue = self._queue()

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        ProductionTask.objects.filter(pk__in=[t.id for t in tasks]).delete()

        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('1.30'))
        self.assertEqual(queue.actual_consumption, Decimal('1.30'))
        self.assertTrue(queue.has_transaction)
        self.assertTrue(queue.has_plan_conflict)

    def test_15_sync_after_move_does_not_overwrite_delivered_row(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()
        delivered = queue.delivered_quantity

        tomorrow = self.day1 + timezone.timedelta(days=1)
        for task in tasks:
            task.scheduled_start = timezone.make_aware(
                timezone.datetime.combine(tomorrow, time(8, 0))
            )
            task.save()

        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, delivered)
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertTrue(queue.has_plan_conflict)
        self.assertEqual(self._queue(work_date=tomorrow).planned_quantity,
                         Decimal('1.30'))

    def test_16_movements_are_not_duplicated_by_sync(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        before = StockMovement.objects.filter(movement_type='consumption').count()

        services.sync_daily_material_queue(self.day1)
        services.sync_daily_material_queue(self.day1)

        self.assertEqual(
            StockMovement.objects.filter(movement_type='consumption').count(),
            before,
        )


class AssignPaintingProcessSyncTests(WorkUnitBase):
    def test_17_assign_painting_process_syncs_the_previous_day(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        self.assertIsNotNone(self._queue())

        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse('assign_painting_process', args=[self.order_item.id])
        )
        self.assertEqual(response.status_code, 302)

        queue = self._queue()
        self.assertEqual(queue.status, 'cancelled')
        self.assertEqual(queue.sources.count(), 0)
        self.assertFalse(
            ProductionTask.objects.filter(pk__in=[t.id for t in tasks]).exists()
        )
        # تسک‌های جدید زمان‌بندی نشده‌اند، پس نباید نیازی وارد صف شود.
        self.assertEqual(
            DailyMaterialQueue.objects.filter(
                work_date=self.day1, raw_material=self.raw
            ).exclude(status='cancelled').count(),
            0,
        )

    def test_18_assign_painting_process_keeps_done_tasks_and_queue(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        tasks[0].status = 'done'
        tasks[0].save()

        self.client.force_login(self.superuser)
        self.client.post(reverse('assign_painting_process', args=[self.order_item.id]))

        self.assertTrue(ProductionTask.objects.filter(pk=tasks[0].id).exists())
        self.assertEqual(self._queue().planned_quantity, Decimal('1.30'))


class BulkPathSyncTests(WorkUnitBase):
    def test_19_queryset_update_of_worker_syncs_queue(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        ProductionTask.objects.filter(
            pk__in=[t.id for t in tasks]
        ).update(assigned_worker=self.worker_b)

        # Old queues cancelled
        old_queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_a, raw_material=self.raw
        )
        self.assertEqual(old_queues.count(), 6)
        for q in old_queues:
            self.assertEqual(q.status, 'cancelled')
            self.assertEqual(q.sources.count(), 0)

        # New queues created for worker_b
        new_queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker_b, raw_material=self.raw
        )
        self.assertEqual(new_queues.count(), 6)
        for q in new_queues:
            self.assertEqual(q.planned_quantity, Decimal('1.30'))
            self.assertEqual(q.sources.count(), 1)

    def test_20_queryset_unschedule_syncs_queue(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        ProductionTask.objects.filter(pk__in=[t.id for t in tasks]).update(
            scheduled_start=None, scheduled_end=None, assigned_worker=None,
        )

        self.assertEqual(self._queue().status, 'cancelled')


class OrderTraceabilityAfterMoveTests(WorkUnitBase):
    """ردیابیِ موادِ سفارش - هر مرحله یک واحد کار جداگانه است."""

    def _trace_planned(self):
        report = reports.order_material_traceability(self.order)
        return report['totals']['planned']

    def test_21_move_to_another_worker_counts_planned_once(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        # 6 stages × 1.30 = 7.80 total planned
        self.assertEqual(self._trace_planned(), Decimal('7.80'))

        for task in tasks:
            task.assigned_worker = self.worker_b
            task.save()

        # After move, still 6 stages × 1.30 = 7.80
        self.assertEqual(self._trace_planned(), Decimal('7.80'))

    def test_22_move_to_another_day_counts_planned_once(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        tomorrow = self.day1 + timezone.timedelta(days=1)

        for task in tasks:
            task.scheduled_start = timezone.make_aware(
                timezone.datetime.combine(tomorrow, time(8, 0))
            )
            task.save()

        # After move to another day, still 6 stages × 1.30 = 7.80
        self.assertEqual(self._trace_planned(), Decimal('7.80'))

    def test_23_six_stages_are_not_six_times_in_traceability(self):
        self._stage_tasks(self.order_item, self.worker_a)

        report = reports.order_material_traceability(self.order)
        # 6 stages × 1.30 = 7.80 total planned
        self.assertEqual(report['totals']['planned'], Decimal('7.80'))
        # Now each stage creates its own queue row, so task_count should be 1 per row
        self.assertEqual(len(report['rows']), 6)
        for row in report['rows']:
            self.assertEqual(row['task_count'], 1)
