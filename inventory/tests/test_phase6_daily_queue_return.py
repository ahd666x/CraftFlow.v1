"""
Phase 6 — برگشت مواد و مصرف واقعی (Return & Actual Consumption).

جریان تحت آزمون:
    صف تحویل‌شده → برگشت فیزیکی → StockMovement(return) → موجودی عمومی انبار
    → returned_quantity به‌روز → actual_consumption = delivered − returned

قواعد کلیدی که این تست‌ها تثبیت می‌کنند:
    * برگشت بیش از delivered، منفی، صفر، تکراری، روی صف تحویل‌نشده یا
      cancelled رد می‌شود؛
    * actual_consumption هرگز منفی نمی‌شود و با برگشت افزایش نمی‌یابد؛
    * planned quantity و منابع برنامه‌ریزی هرگز تغییر نمی‌کنند؛
    * برگشت به امانت/رزرو کارگر نمی‌رود — مقصد، موجودی عمومی انبار است؛
    * عملیات atomic است و در خطا کامل rollback می‌شود؛
    * درخواست همزمان، برگشت مضاعف نمی‌سازد (select_for_update).
"""
import json
from datetime import time
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory import services
from inventory.models import (
    DailyMaterialQueue,
    MaterialCustody,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
)
from inventory.services import HandoverError
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


class ReturnBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('p6admin', password='pw')
        cls.warehouse_user = User.objects.create_user('p6warehouse', password='pw')
        group, _ = Group.objects.get_or_create(name='انبار')
        cls.warehouse_user.groups.add(group)
        cls.plain_user = User.objects.create_user('p6plain', password='pw')

        cls.worker = User.objects.create_user('p6worker', password='pw', first_name='حسین')
        cls.other_worker = User.objects.create_user('p6worker2', password='pw', first_name='علی')

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
            name='روند نقاشی', code='P6', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='زیرکار',
            duration_minutes=30, drying_time_minutes=0,
        )
        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.superuser, customer=cls.customer, number='P6-1',
        )
        cls.order_item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=2,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)

        # فرمول مصرف نقاشی تا تسک واقعاً وارد صف روزانه شود
        PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.raw, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.500'),
        )

        StockMovement.objects.create(
            raw_material=cls.raw, movement_type='purchase',
            quantity=Decimal('100'), created_by=cls.superuser,
        )

        cls.today_date = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.today_date, time(8, 0))
        )

    def _make_task(self, *, quantity=2):
        return ProductionTask.objects.create(
            order=self.order,
            order_item=self.order_item,
            station_name='paint',
            step_order=1,
            quantity=quantity,
            status='pending',
            painting_stage=self.stage,
            color_part='بدنه',
            assigned_worker=self.worker,
            scheduled_start=self.day_start,
            scheduled_end=None,
        )

    def _queue(self):
        return DailyMaterialQueue.objects.filter(
            work_date=self.today_date,
            worker=self.worker,
            raw_material=self.raw,
        ).first()

    def _build_queue(self, planned='4', *, worker=None):
        return DailyMaterialQueue.objects.create(
            work_date=self.today_date,
            worker=worker or self.worker,
            raw_material=self.raw,
            planned_quantity=Decimal(planned),
            status='pending',
        )

    def _delivered_queue(self, planned='4'):
        """صفی که تحویل شده و آمادهٔ برگشت است."""
        queue = self._build_queue(planned)
        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.superuser,
        )
        queue.refresh_from_db()
        return queue

    def _return(self, queue, quantity):
        return services.execute_daily_return(
            queue_id=queue.pk,
            returned_by=self.superuser,
            returned_quantity=quantity,
        )

    def _ajax(self):
        return {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}

    def _post_return(self, queue, quantity, user=None):
        self.client.force_login(user or self.warehouse_user)
        return self.client.post(
            reverse('inventory:daily_queue_return', args=[queue.pk]),
            {'returned_quantity': str(quantity)},
            **self._ajax(),
        )


class ReturnValidationTests(ReturnBase):
    def test_01_return_more_than_delivered_rejected(self):
        queue = self._delivered_queue('4')
        stock_before = self.raw.current_stock

        with self.assertRaises(HandoverError) as ctx:
            self._return(queue, '5')

        self.assertIn('نمی‌تواند از مقدار تحویل‌شده', str(ctx.exception))
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))
        self.assertEqual(self.raw.current_stock, stock_before)

    def test_02_return_exactly_delivered_accepted(self):
        queue = self._delivered_queue('4')

        self._return(queue, '4')

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('4.00'))
        self.assertEqual(queue.status, 'returned')

    def test_03_return_zero_rejected(self):
        queue = self._delivered_queue('4')

        with self.assertRaises(HandoverError) as ctx:
            self._return(queue, '0')

        self.assertIn('بزرگ‌تر از صفر', str(ctx.exception))

    def test_04_return_negative_rejected(self):
        queue = self._delivered_queue('4')

        with self.assertRaises(HandoverError) as ctx:
            self._return(queue, '-1')

        self.assertIn('بزرگ‌تر از صفر', str(ctx.exception))
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))

    def test_05_return_non_numeric_rejected(self):
        queue = self._delivered_queue('4')

        with self.assertRaises(HandoverError) as ctx:
            self._return(queue, 'abc')

        self.assertIn('معتبر نیست', str(ctx.exception))

    def test_06_duplicate_full_return_rejected(self):
        queue = self._delivered_queue('4')
        self._return(queue, '4')
        stock_after_first = self.raw.current_stock
        movements_after_first = StockMovement.objects.count()

        with self.assertRaises(HandoverError) as ctx:
            self._return(queue, '1')

        self.assertIn('قبلاً برگشت داده شده', str(ctx.exception))
        self.assertEqual(self.raw.current_stock, stock_after_first)
        self.assertEqual(StockMovement.objects.count(), movements_after_first)

    def test_07_return_on_undelivered_queue_rejected(self):
        queue = self._build_queue('4')

        with self.assertRaises(HandoverError) as ctx:
            self._return(queue, '1')

        self.assertIn('هنوز تحویل نشده', str(ctx.exception))
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw, movement_type='return'
            ).count(),
            0,
        )

    def test_08_return_on_cancelled_queue_rejected(self):
        queue = self._delivered_queue('4')
        queue.status = 'cancelled'
        queue.save(update_fields=['status'])
        stock_before = self.raw.current_stock

        with self.assertRaises(HandoverError) as ctx:
            self._return(queue, '1')

        self.assertIn('از برنامهٔ روز حذف شده', str(ctx.exception))
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'cancelled')
        self.assertEqual(self.raw.current_stock, stock_before)

    def test_09_return_on_missing_queue_rejected(self):
        with self.assertRaises(HandoverError) as ctx:
            services.execute_daily_return(
                queue_id=999999, returned_by=self.superuser, returned_quantity='1',
            )
        self.assertIn('یافت نشد', str(ctx.exception))


class PartialReturnTests(ReturnBase):
    def test_10_partial_return_is_supported(self):
        queue = self._delivered_queue('4')

        self._return(queue, '1')

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('1.00'))
        self.assertEqual(queue.status, 'delivered')

        self._return(queue, '1.5')

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('2.50'))
        self.assertEqual(queue.status, 'delivered')

    def test_11_partial_returns_sum_cannot_exceed_delivered(self):
        queue = self._delivered_queue('4')
        self._return(queue, '3')

        with self.assertRaises(HandoverError):
            self._return(queue, '1.50')

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('3.00'))

    def test_12_last_partial_return_closes_queue(self):
        queue = self._delivered_queue('4')
        self._return(queue, '3')
        self._return(queue, '1')

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('4.00'))
        self.assertEqual(queue.status, 'returned')
        self.assertEqual(queue.actual_consumption, Decimal('0.00'))


class ActualConsumptionTests(ReturnBase):
    def test_13_actual_consumption_is_delivered_minus_returned(self):
        queue = self._delivered_queue('5')
        self.assertEqual(queue.actual_consumption, Decimal('5.00'))

        self._return(queue, '2')

        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('3.00'))

        self._return(queue, '1.25')

        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('1.75'))

    def test_14_return_never_increases_actual_consumption(self):
        queue = self._delivered_queue('4')
        before = queue.actual_consumption

        self._return(queue, '1')

        queue.refresh_from_db()
        self.assertLess(queue.actual_consumption, before)
        self.assertEqual(queue.actual_consumption, Decimal('3.00'))

    def test_15_actual_consumption_never_negative(self):
        queue = self._delivered_queue('4')
        self._return(queue, '4')

        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('0.00'))
        self.assertGreaterEqual(queue.actual_consumption, Decimal('0.00'))
        self.assertGreaterEqual(queue.excess_consumption, Decimal('0.00'))

    def test_16_actual_consumption_below_planned_gives_no_excess(self):
        queue = self._delivered_queue('4')
        self._return(queue, '1')

        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('3.00'))
        self.assertEqual(queue.excess_consumption, Decimal('0.00'))

    def test_17_return_smaller_than_planned_gives_no_excess(self):
        """مصرف کمتر از برنامه، مصرف اضافه نیست."""
        queue = self._delivered_queue('10')
        self._return(queue, '2')

        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('8.00'))
        self.assertEqual(queue.excess_consumption, Decimal('0.00'))

    def test_18_recalculate_commits_correct_values(self):
        queue = self._delivered_queue('4')
        self._return(queue, '1.5')

        queue.refresh_from_db()
        queue.actual_consumption = Decimal('999')
        queue.excess_consumption = Decimal('999')
        changed = queue.recalculate_consumption(commit=True)

        self.assertTrue(changed)
        queue.refresh_from_db()
        self.assertEqual(queue.actual_consumption, Decimal('2.50'))
        self.assertEqual(queue.excess_consumption, Decimal('0.00'))

    def test_19_plan_conflict_does_not_block_return(self):
        """تعارض برنامه مانع برگشت واقعی نمی‌شود."""
        queue = self._delivered_queue('4')
        self._return(queue, '1')
        queue.refresh_from_db()
        queue.planned_quantity = Decimal('20')
        queue.has_plan_conflict = True
        queue.conflict_note = 'تغییر برنامه'
        queue.save(update_fields=[
            'planned_quantity', 'has_plan_conflict', 'conflict_note',
        ])

        result = self._return(queue, '1')

        self.assertEqual(result.returned_quantity, Decimal('2.00'))
        self.assertEqual(result.actual_consumption, Decimal('2.00'))


class ReturnInventoryTests(ReturnBase):
    def test_20_return_creates_stock_movement_return(self):
        queue = self._delivered_queue('4')

        self._return(queue, '1.25')

        movements = StockMovement.objects.filter(
            raw_material=self.raw, movement_type='return'
        )
        self.assertEqual(movements.count(), 1)
        movement = movements.get()
        self.assertEqual(movement.quantity, Decimal('1.25'))
        self.assertEqual(movement.created_by, self.superuser)

    def test_21_return_increases_general_stock(self):
        queue = self._delivered_queue('4')
        stock_after_delivery = self.raw.current_stock

        self._return(queue, '1')

        self.assertEqual(self.raw.current_stock, stock_after_delivery + Decimal('1.00'))

    def test_22_return_does_not_create_worker_custody(self):
        """برگشت به امانت کارگر نمی‌رود."""
        queue = self._delivered_queue('4')

        self._return(queue, '2')

        self.assertFalse(
            MaterialCustody.objects.exists(),
            'برگشت نباید برای کارگر امانت بسازد.',
        )
        self.assertFalse(
            self.raw.custodies.exists(),
            'برگشت نباید به ماده کارگر تخصیص داده شود.',
        )

    def test_23_existing_custody_is_not_touched_by_return(self):
        custody = MaterialCustody.objects.create(
            raw_material=self.raw, held_by=self.other_worker, quantity=Decimal('3'),
        )
        queue = self._delivered_queue('4')

        self._return(queue, '1')

        custody.refresh_from_db()
        self.assertEqual(custody.quantity, Decimal('3.00'))

    def test_24_multiple_returns_create_separate_movements(self):
        queue = self._delivered_queue('4')

        self._return(queue, '1')
        self._return(queue, '1')
        self._return(queue, '1')

        movements = StockMovement.objects.filter(
            raw_material=self.raw, movement_type='return'
        )
        self.assertEqual(movements.count(), 3)
        total = sum(m.quantity for m in movements)
        self.assertEqual(total, Decimal('3.00'))

    def test_25_no_new_movement_type_is_introduced(self):
        queue = self._delivered_queue('4')
        self._return(queue, '1')

        allowed = {choice[0] for choice in StockMovement.MOVEMENT_TYPES}
        used = set(
            StockMovement.objects.values_list('movement_type', flat=True).distinct()
        )
        self.assertTrue(used.issubset(allowed), used - allowed)


class ReturnPlanningIntegrityTests(ReturnBase):
    def test_26_planned_quantity_and_sources_unchanged(self):
        self._make_task(quantity=4)
        queue = self._queue()
        self.assertIsNotNone(queue)
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.superuser,
        )
        queue.refresh_from_db()

        sources_before = list(
            queue.sources.values_list('production_task_id', 'painting_stage_id', 'quantity')
        )
        task_ids_before = list(
            queue.sources.values_list('production_task_id', flat=True)
        )

        self._return(queue, '0.50')

        queue.refresh_from_db()
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        self.assertEqual(
            list(queue.sources.values_list('production_task_id', 'painting_stage_id', 'quantity')),
            sources_before,
        )
        self.assertEqual(
            list(queue.sources.values_list('production_task_id', flat=True)),
            task_ids_before,
        )

    def test_27_delivered_quantity_unchanged_by_return(self):
        queue = self._delivered_queue('4')

        self._return(queue, '1.5')

        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('4.00'))

    def test_28_return_via_real_queue_from_painting_schedule(self):
        """جریان کامل: تسک نقاشی → صف → تحویل → برگشت."""
        self._make_task(quantity=4)
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.superuser,
        )
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'delivered')

        self._return(queue, '0.75')

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(queue.planned_quantity, Decimal('2.00'))
        self.assertEqual(queue.delivered_quantity, Decimal('2.00'))
        self.assertEqual(queue.returned_quantity, Decimal('0.75'))
        self.assertEqual(queue.actual_consumption, Decimal('1.25'))
        self.assertEqual(queue.sources.count(), 1)


class ReturnAtomicityTests(ReturnBase):
    def test_29_rollback_when_queue_save_fails(self):
        queue = self._delivered_queue('4')
        stock_before = self.raw.current_stock
        movements_before = StockMovement.objects.count()

        with mock.patch.object(DailyMaterialQueue, 'save', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._return(queue, '1')

        self.assertEqual(StockMovement.objects.count(), movements_before)
        self.assertEqual(self.raw.current_stock, stock_before)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))

    def test_30_rollback_when_movement_create_fails(self):
        queue = self._delivered_queue('4')
        stock_before = self.raw.current_stock

        with mock.patch.object(StockMovement, 'objects') as mock_manager:
            mock_manager.create.side_effect = RuntimeError('movement boom')
            with self.assertRaises(RuntimeError):
                self._return(queue, '1')

        self.assertEqual(self.raw.current_stock, stock_before)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))

    def test_31_failed_return_leaves_queue_fully_consistent(self):
        queue = self._delivered_queue('4')
        queue_before = (
            queue.status, queue.delivered_quantity, queue.returned_quantity,
            queue.actual_consumption, queue.planned_quantity,
        )

        with self.assertRaises(HandoverError):
            self._return(queue, '99')

        queue.refresh_from_db()
        self.assertEqual(
            (
                queue.status, queue.delivered_quantity, queue.returned_quantity,
                queue.actual_consumption, queue.planned_quantity,
            ),
            queue_before,
        )

    def test_32_select_for_update_is_used(self):
        """قفل ردیف صف، مبنای محافظت از درخواست همزمان است."""
        with mock.patch(
            'inventory.models.DailyMaterialQueue.objects.select_for_update'
        ) as mock_lock:
            mock_lock.return_value.get.side_effect = DailyMaterialQueue.DoesNotExist
            with self.assertRaises(HandoverError):
                services.execute_daily_return(
                    queue_id=1, returned_by=self.superuser, returned_quantity='1',
                )
        mock_lock.assert_called()


class ReturnDuplicateRequestTests(ReturnBase):
    def test_33_repeated_post_creates_one_movement(self):
        """تکرار همان درخواست برگشت نباید موجودی را دوباره افزایش دهد."""
        queue = self._delivered_queue('4')
        stock_after_delivery = self.raw.current_stock

        first = self._post_return(queue, '4')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.raw.current_stock, stock_after_delivery + Decimal('4.00'))

        # همان درخواست دوباره: چیزی برای برگرداندن باقی نمانده است
        second = self._post_return(queue, '4')
        self.assertEqual(second.status_code, 400)
        payload = json.loads(second.content)
        self.assertFalse(payload['success'])

        # دقیقاً یک حرکت برگشت
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw, movement_type='return'
            ).count(),
            1,
        )
        self.assertEqual(self.raw.current_stock, stock_after_delivery + Decimal('4.00'))
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('4.00'))
        self.assertEqual(queue.actual_consumption, Decimal('0.00'))

    def test_33b_consecutive_partial_posts_accumulate_correctly(self):
        """POSTهای جزئی پشت‌سرهم مجازند و درست جمع می‌شوند."""
        queue = self._delivered_queue('4')
        stock_after_delivery = self.raw.current_stock

        self.assertEqual(self._post_return(queue, '1').status_code, 200)
        self.assertEqual(self._post_return(queue, '1').status_code, 200)
        self.assertEqual(self._post_return(queue, '1').status_code, 200)

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('3.00'))
        self.assertEqual(queue.actual_consumption, Decimal('1.00'))
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw, movement_type='return'
            ).count(),
            3,
        )
        self.assertEqual(self.raw.current_stock, stock_after_delivery + Decimal('3.00'))

        # درخواست چهارم که از باقی‌مانده بیشتر است رد می‌شود
        response = self._post_return(queue, '2')
        self.assertEqual(response.status_code, 400)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('3.00'))
        self.assertEqual(self.raw.current_stock, stock_after_delivery + Decimal('3.00'))

    def test_34_post_return_over_delivered_rejected(self):
        queue = self._delivered_queue('4')

        response = self._post_return(queue, '10')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(json.loads(response.content)['success'])
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw, movement_type='return'
            ).count(),
            0,
        )

    def test_35_post_return_on_undelivered_rejected(self):
        queue = self._build_queue('4')

        response = self._post_return(queue, '1')

        self.assertEqual(response.status_code, 400)
        payload = json.loads(response.content)
        self.assertIn('هنوز تحویل نشده', payload['error'])

    def test_36_post_return_missing_quantity_rejected(self):
        queue = self._delivered_queue('4')
        self.client.force_login(self.warehouse_user)

        response = self.client.post(
            reverse('inventory:daily_queue_return', args=[queue.pk]),
            {'returned_quantity': ''},
            **self._ajax(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(json.loads(response.content)['success'])

    def test_37_return_success_payload_is_persian_friendly(self):
        queue = self._delivered_queue('4')

        response = self._post_return(queue, '1')

        payload = json.loads(response.content)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['returned_quantity'], '1.00')
        self.assertEqual(payload['actual_consumption'], '3.00')
        self.assertEqual(payload['status'], 'delivered')

        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('موفقیت' in m for m in messages), messages)


class ReturnPermissionTests(ReturnBase):
    def test_38_unauthorized_user_cannot_return(self):
        queue = self._delivered_queue('4')
        stock_before = self.raw.current_stock

        self.client.force_login(self.plain_user)
        response = self.client.post(
            reverse('inventory:daily_queue_return', args=[queue.pk]),
            {'returned_quantity': '1'},
            **self._ajax(),
        )

        self.assertEqual(response.status_code, 403)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))
        self.assertEqual(self.raw.current_stock, stock_before)

    def test_39_anonymous_user_cannot_return(self):
        queue = self._delivered_queue('4')

        response = self.client.post(
            reverse('inventory:daily_queue_return', args=[queue.pk]),
            {'returned_quantity': '1'},
            **self._ajax(),
        )

        self.assertEqual(response.status_code, 302)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))

    def test_40_missing_ajax_header_rejected(self):
        queue = self._delivered_queue('4')
        self.client.force_login(self.warehouse_user)

        response = self.client.post(
            reverse('inventory:daily_queue_return', args=[queue.pk]),
            {'returned_quantity': '1'},
        )

        self.assertEqual(response.status_code, 403)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))

    def test_41_warehouse_user_can_return(self):
        queue = self._delivered_queue('4')

        response = self._post_return(queue, '1', user=self.warehouse_user)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)['success'])
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('1.00'))