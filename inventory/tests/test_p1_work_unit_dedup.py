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
        PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.raw, product=cls.product,
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


class MultiStageDedupTests(WorkUnitBase):
    def test_02_two_stages_count_requirement_once(self):
        self._stage_tasks(self.order_item, self.worker_a, stages=self.stages[:2])

        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue.sources.count(), 2)

    def test_03_six_stages_count_requirement_once(self):
        """سناریوی خواسته‌شده: ۶ مرحله، نیاز ۱٫۳ کیلو، نه ۷٫۸ و نه ۲٫۶."""
        self._stage_tasks(self.order_item, self.worker_a)

        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue.sources.count(), 6)

    def test_04_sources_keep_traceability_and_sum_matches_planned(self):
        """منبع‌ها همهٔ taskها را نشان می‌دهند ولی جمعشان برابر مقدار صف است."""
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        queue = self._queue()
        self.assertEqual(
            {s.production_task_id for s in queue.sources.all()},
            {t.id for t in tasks},
        )
        self.assertEqual(
            sum((s.quantity for s in queue.sources.all()), Decimal('0')),
            queue.planned_quantity,
        )

    def test_05_unit_quantity_scales_with_production_quantity(self):
        """۶ مرحله با تعداد ۲ واحد: نیاز ۲٫۶، نه ۶ برابر ۲٫۶."""
        self._stage_tasks(self.order_item, self.worker_a, quantity=2)

        self.assertEqual(self._queue().planned_quantity, Decimal('2.60'))

    def test_06_six_stages_of_two_colour_parts_stay_separate(self):
        """قطعهٔ رنگی متفاوت = واحد کار متفاوت = نیاز مستقل."""
        PaintingMaterialRequirement.objects.create(
            process=self.process, raw_material=self.raw, product=self.product,
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

        self.assertEqual(self._queue().planned_quantity, Decimal('2.60'))
        self.assertEqual(self._queue().sources.count(), 6)


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
    def test_09_worker_change_moves_the_queue_row(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        for task in tasks:
            task.assigned_worker = self.worker_b
            task.save()

        old_queue = self._queue(worker=self.worker_a)
        new_queue = self._queue(worker=self.worker_b)
        self.assertEqual(old_queue.status, 'cancelled')
        self.assertEqual(new_queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(new_queue.sources.count(), 6)

    def test_10_date_change_moves_the_queue_row(self):
        tomorrow = self.day1 + timezone.timedelta(days=1)
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        for task in tasks:
            task.scheduled_start = timezone.make_aware(
                timezone.datetime.combine(tomorrow, time(8, 0))
            )
            task.save()

        self.assertEqual(self._queue().status, 'cancelled')
        moved = self._queue(work_date=tomorrow)
        self.assertEqual(moved.planned_quantity, Decimal('1.30'))
        self.assertEqual(moved.sources.count(), 6)

    def test_11_stage_change_keeps_planned_quantity_stable(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        for task in tasks:
            task.painting_stage = self.stages[0]
            task.save()

        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue.sources.count(), 6)

    def test_12_deleting_all_tasks_removes_planned_quantity(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        before = self._queue()
        self.assertIsNotNone(before)
        self.assertEqual(before.sources.count(), len(tasks))

        ProductionTask.objects.filter(pk__in=[t.id for t in tasks]).delete()

        queue = self._queue()
        self.assertEqual(queue.status, 'cancelled')
        self.assertEqual(queue.sources.count(), 0)
        self.assertEqual(queue.delivered_quantity, Decimal('0.00'))

    def test_13_deleting_one_stage_keeps_need_and_sources_consistent(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        tasks[0].delete()

        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue.sources.count(), 5)
        self.assertEqual(
            sum((s.quantity for s in queue.sources.all()), Decimal('0')),
            queue.planned_quantity,
        )


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

        self.assertEqual(self._queue(worker=self.worker_a).status, 'cancelled')
        self.assertEqual(self._queue(worker=self.worker_a).sources.count(), 0)
        moved = self._queue(worker=self.worker_b)
        self.assertEqual(moved.planned_quantity, Decimal('1.30'))
        self.assertEqual(moved.sources.count(), len(tasks))

    def test_20_queryset_unschedule_syncs_queue(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)

        ProductionTask.objects.filter(pk__in=[t.id for t in tasks]).update(
            scheduled_start=None, scheduled_end=None, assigned_worker=None,
        )

        self.assertEqual(self._queue().status, 'cancelled')


class OrderTraceabilityAfterMoveTests(WorkUnitBase):
    """ردیابیِ موادِ سفارش بعد از جابه‌جایی نباید نیاز را دوبار بشمارد."""

    def _trace_planned(self):
        report = reports.order_material_traceability(self.order)
        return report['totals']['planned']

    def test_21_move_to_another_worker_counts_planned_once(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        self.assertEqual(self._trace_planned(), Decimal('1.30'))

        for task in tasks:
            task.assigned_worker = self.worker_b
            task.save()

        self.assertEqual(self._trace_planned(), Decimal('1.30'))

    def test_22_move_to_another_day_counts_planned_once(self):
        tasks = self._stage_tasks(self.order_item, self.worker_a)
        tomorrow = self.day1 + timezone.timedelta(days=1)

        for task in tasks:
            task.scheduled_start = timezone.make_aware(
                timezone.datetime.combine(tomorrow, time(8, 0))
            )
            task.save()

        self.assertEqual(self._trace_planned(), Decimal('1.30'))

    def test_23_six_stages_are_not_six_times_in_traceability(self):
        self._stage_tasks(self.order_item, self.worker_a)

        report = reports.order_material_traceability(self.order)
        self.assertEqual(report['totals']['planned'], Decimal('1.30'))
        self.assertEqual(report['rows'][0]['task_count'], 6)
