"""
Phase 8 — کنترل و بستن روز (Daily Closing / Final Control).

این Phase فقط «نمای کنترلی» است:Closing نه ``StockMovement`` می‌سازد، نه صف را
تغییر می‌دهد و نه تحویل/برگشتی اجرا می‌کند. همهٔ اعداد از همان سرویس‌های
گزارش Phase 7 خوانده می‌شوند.
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

import jdatetime

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


class ClosingBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('p8admin', password='pw')
        cls.warehouse_user = User.objects.create_user('p8warehouse', password='pw')
        group, _ = Group.objects.get_or_create(name='انبار')
        cls.warehouse_user.groups.add(group)
        cls.plain_user = User.objects.create_user('p8plain', password='pw')

        cls.worker = User.objects.create_user('p8worker', password='pw', first_name='حسین')
        cls.worker_b = User.objects.create_user('p8worker_b', password='pw', first_name='علی')

        cls.raw_category = RawMaterialCategory.objects.create(name='مواد نقاشی')
        cls.raw = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ سفید', code='P8-WHITE',
            unit='kg', pack_size=Decimal('0'),
        )
        cls.raw_b = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ کیلر', code='P8-KILLER',
            unit='kg', pack_size=Decimal('0'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته تست')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول تست', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند نقاشی', code='P8', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='زیرکار',
            duration_minutes=30, drying_time_minutes=0,
        )
        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.superuser, customer=cls.customer, number='P8-1',
        )
        cls.order_item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=2,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)

        for raw in (cls.raw, cls.raw_b):
            PaintingMaterialRequirement.objects.create(
                process=cls.process, raw_material=raw, product=cls.product,
                color_part='بدنه', consumption_per_unit=Decimal('1.000'),
            )
            StockMovement.objects.create(
                raw_material=raw, movement_type='purchase',
                quantity=Decimal('100'), created_by=cls.superuser,
            )

        cls.today = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.today, time(8, 0))
        )

    def _task(self, *, quantity=2, worker=None, start=None):
        return ProductionTask.objects.create(
            order=self.order,
            order_item=self.order_item,
            station_name='paint',
            step_order=1,
            quantity=quantity,
            status='pending',
            painting_stage=self.stage,
            color_part='بدنه',
            assigned_worker=worker or self.worker,
            scheduled_start=start or self.day_start,
            scheduled_end=None,
        )

    def _queue(self, raw=None, worker=None, date=None):
        return DailyMaterialQueue.objects.filter(
            work_date=date or self.today,
            worker=worker or self.worker,
            raw_material=raw or self.raw,
        ).first()

    def _closing(self, **kwargs):
        return services.daily_closing_status(self.today, **kwargs)

    def _day_queues(self, worker=None, date=None):
        return list(
            DailyMaterialQueue.objects.filter(
                work_date=date or self.today,
                **({'worker': worker} if worker else {}),
            )
        )

    def _deliver(self, queue):
        """تحویل یک ردیف و برگرداندن نسخهٔ تازه (نمونهٔ درون‌حافظه‌ای کهنه است)."""
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()
        return queue

    def _return(self, queue, quantity):
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser,
            returned_quantity=quantity,
        )
        queue.refresh_from_db()
        return queue

    def _settle_day(self, **kwargs):
        """همهٔ ردیف‌های روز را تحویل و کامل برگردان (روز تمیز)."""
        for queue in self._day_queues(**kwargs):
            self._deliver(queue)
            self._return(queue, queue.delivered_quantity)

    def _get(self, params=None, user=None):
        self.client.force_login(user or self.warehouse_user)
        return self.client.get(reverse('inventory:daily_closing'), params or {})


class ClosingStatusTests(ClosingBase):
    def test_01_clean_day_is_closable(self):
        # همهٔ ردیف‌ها تحویل و برگشت کامل دارند ⇒ مشکلی نیست
        self._task(quantity=2)
        self._settle_day()

        control = self._closing()
        self.assertEqual(control['status'], 'closable')
        self.assertTrue(control['closable'])
        self.assertEqual(control['problem_count'], 0)
        self.assertEqual(control['problems'], [])

    def test_02_conflict_makes_day_needs_review(self):
        task = self._task(quantity=2)
        self._settle_day()

        # تغییر برنامه پس از تراکنش ⇒ تعارض روی ردیف‌های همان تسک
        task.quantity = 20
        task.save()
        conflicted = [q for q in self._day_queues() if q.has_plan_conflict]
        self.assertTrue(conflicted)

        control = self._closing()
        self.assertEqual(control['status'], 'needs_review')
        self.assertFalse(control['closable'])
        self.assertEqual(control['conflict_count'], len(conflicted))
        self.assertGreaterEqual(control['problem_count'], 1)

    def test_03_incomplete_row_makes_day_needs_review(self):
        # نیاز برنامه‌ریزی‌شده هست ولی تحویل نشده ⇒ ردیف ناقص
        self._task(quantity=2)
        self.assertTrue(
            all(q.status == 'pending' for q in self._day_queues())
        )

        control = self._closing()
        self.assertEqual(control['status'], 'needs_review')
        self.assertEqual(control['incomplete_count'], 2)

    def test_04_delivered_without_return_is_closable(self):
        """تحویل بدون برگشت در پایان روز وضعیت عادی است، نه مشکل."""
        self._task(quantity=2)
        for queue in self._day_queues():
            self._deliver(queue)

        control = self._closing()
        self.assertEqual(control['status'], 'closable')
        self.assertEqual(control['problem_count'], 0)

    def test_05_inconsistent_transaction_state_needs_review(self):
        """وضعیت ذخیره‌شده با مقدارهای تحویل/برگشت نمی‌خواند."""
        self._task(quantity=2)
        queue = self._queue()
        # delivered=0 ولی status='delivered' ⇒ ناسازگار با has_transaction
        queue.status = 'delivered'
        queue.save(update_fields=['status'])

        control = self._closing()
        self.assertEqual(control['status'], 'needs_review')
        self.assertEqual(control['inconsistent_count'], 1)

    def test_06_returned_status_with_partial_return_is_inconsistent(self):
        self._task(quantity=2)
        queue = self._queue()
        self._deliver(queue)
        self._return(queue, '0.50')
        # status='returned' فقط وقتی معتبر است که همهٔ تحویل برگشته باشد
        self.assertEqual(queue.status, 'delivered')
        queue.status = 'returned'
        queue.save(update_fields=['status'])

        control = self._closing()
        self.assertEqual(control['status'], 'needs_review')
        self.assertEqual(control['inconsistent_count'], 1)

    def test_07_problem_reasons_are_reported(self):
        self._task(quantity=2)
        queue = self._queue()
        queue.has_plan_conflict = True
        queue.conflict_note = 'برنامه پس از تحویل تغییر کرد'
        queue.save(update_fields=['has_plan_conflict', 'conflict_note'])

        control = self._closing()
        flagged = next(
            p for p in control['problems'] if p['queue'].pk == queue.pk
        )
        self.assertIn('conflict', flagged['reasons'])
        self.assertIn('incomplete', flagged['reasons'])

    def test_08_cancelled_rows_are_not_problems(self):
        self._task(quantity=2)
        for queue in self._day_queues():
            queue.status = 'cancelled'
            queue.save(update_fields=['status'])

        control = self._closing()
        self.assertEqual(control['status'], 'closable')
        self.assertEqual(control['problem_count'], 0)


class ClosingTotalsTests(ClosingBase):
    def test_09_closing_reports_correct_totals(self):
        self._task(quantity=2)
        for queue in self._day_queues():
            self._deliver(queue)
            self._return(queue, '0.50')

        summary = self._closing()['summary']
        # planned = 2 × 2.00 (هر تسک برای هر دو ماده ردیف می‌سازد)
        self.assertEqual(summary['total_planned'], Decimal('4.00'))
        self.assertEqual(summary['total_delivered'], Decimal('4.00'))
        self.assertEqual(summary['total_returned'], Decimal('1.00'))
        self.assertEqual(summary['total_actual'], Decimal('3.00'))

    def test_10_closing_matches_phase7_report(self):
        self._task(quantity=2)
        for queue in self._day_queues():
            self._deliver(queue)
            self._return(queue, '1.00')

        closing = self._closing()['summary']
        report = services.daily_material_report(self.today)['summary']
        for key in ('total_planned', 'total_delivered', 'total_returned',
                    'total_actual'):
            self.assertEqual(closing[key], report[key], f'اختلاف در {key}')

    def test_11_closing_does_not_recalculate_consumption(self):
        self._task(quantity=2)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        # مقدار ذخیره‌شده عمداً دست‌کاری می‌شود؛ Closing نباید آن را بازنویسی کند
        queue.actual_consumption = Decimal('7.77')
        queue.save(update_fields=['actual_consumption'])

        summary = self._closing()['summary']
        self.assertEqual(
            summary['total_actual'], Decimal('7.77'),
            'Closing باید همان فیلد ذخیره‌شدهٔ صف را نشان دهد.',
        )

    def test_12_empty_day_has_zero_totals(self):
        summary = self._closing()['summary']
        self.assertEqual(summary['total_planned'], Decimal('0.00'))
        self.assertEqual(summary['total_actual'], Decimal('0.00'))


class ClosingReadOnlyTests(ClosingBase):
    def _state(self):
        return {
            queue.pk: (
                queue.planned_quantity, queue.delivered_quantity,
                queue.returned_quantity, queue.actual_consumption,
                queue.excess_consumption, queue.status,
                queue.has_plan_conflict, queue.conflict_note,
                queue.updated_at,
            )
            for queue in DailyMaterialQueue.objects.all()
        }

    def _sources_state(self):
        return sorted(
            (s.queue_id, s.production_task_id, str(s.quantity))
            for s in DailyMaterialQueueSource.objects.all()
        )

    def test_13_closing_creates_no_stock_movement(self):
        self._task(quantity=2)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.50',
        )

        before = list(
            StockMovement.objects.values_list('id', 'movement_type', 'quantity')
        )
        for _ in range(3):
            self._closing()
        self._get()

        self.assertEqual(
            list(StockMovement.objects.values_list('id', 'movement_type', 'quantity')),
            before,
            'کنترل و بستن روز نباید StockMovement بسازد.',
        )

    def test_14_closing_does_not_change_queue_rows(self):
        self._task(quantity=2)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.50',
        )

        before = self._state()
        sources_before = self._sources_state()

        for _ in range(3):
            self._closing()
        self._get()

        self.assertEqual(self._state(), before)
        self.assertEqual(self._sources_state(), sources_before)

        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('2.00'))
        self.assertEqual(queue.returned_quantity, Decimal('0.50'))
        self.assertEqual(queue.actual_consumption, Decimal('1.50'))

    def test_15_closing_page_does_not_write(self):
        self._task(quantity=2)
        before = self._state()
        movements_before = StockMovement.objects.count()

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._state(), before)
        self.assertEqual(StockMovement.objects.count(), movements_before)

    def test_16_closing_service_only_reads(self):
        """هیچ مسیر نوشتنی در سرویس Closing صدا زده نمی‌شود."""
        from unittest import mock

        self._task(quantity=2)
        with mock.patch.object(StockMovement.objects, 'create') as create:
            with mock.patch.object(
                DailyMaterialQueue.objects, 'bulk_create'
            ) as bulk:
                self._closing()
        create.assert_not_called()
        bulk.assert_not_called()

    def test_17_closing_does_not_deliver_or_return(self):
        self._task(quantity=2)
        queue = self._queue()
        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('0.00'))
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))

        self._closing()
        queue.refresh_from_db()

        self.assertEqual(queue.delivered_quantity, Decimal('0.00'))
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))
        self.assertEqual(queue.status, 'pending')


class ClosingViewTests(ClosingBase):
    def test_18_empty_day_renders(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/daily_closing.html')
        self.assertTrue(response.context['control']['closable'])
        self.assertContains(response, 'قابل بستن')

    def test_19_day_with_problems_shows_needs_review(self):
        self._task(quantity=2)
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['control']['status'], 'needs_review')
        self.assertContains(response, 'نیازمند بررسی')
        self.assertContains(response, 'ناقص')

    def test_20_problem_rows_are_listed(self):
        self._task(quantity=2)
        response = self._get()

        problems = response.context['problems']
        self.assertTrue(problems)
        self.assertEqual(len(problems), response.context['control']['problem_count'])
        for problem in problems:
            self.assertTrue(problem['reasons'])

    def test_21_date_filter_uses_selected_jalali_date(self):
        tomorrow = self.today + timezone.timedelta(days=1)
        start = timezone.make_aware(timezone.datetime.combine(tomorrow, time(8, 0)))
        self._task(quantity=2, start=start)
        tomorrow_jalali = jdatetime.date.fromgregorian(date=tomorrow)

        response = self._get({'date': tomorrow_jalali.strftime('%Y-%m-%d')})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_gregorian'], tomorrow)
        self.assertEqual(
            response.context['date_str'], tomorrow_jalali.strftime('%Y-%m-%d')
        )
        # ردیف فردا ناقص است، پس روز فردا نیازمند بررسی
        self.assertEqual(response.context['control']['status'], 'needs_review')
        self.assertEqual(response.context['summary']['total_planned'], Decimal('4.00'))
        # ردیف‌های امروز نباید در گزارش فردا باشند
        self.assertEqual(
            {p['queue'].work_date for p in response.context['problems']}, {tomorrow}
        )

    def test_22_today_is_the_default_date(self):
        response = self._get()
        self.assertEqual(response.context['selected_gregorian'], self.today)

    def test_23_invalid_date_falls_back_to_today(self):
        response = self._get({'date': 'نامعتبر'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_gregorian'], self.today)

    def test_24_worker_filter_narrows_the_day(self):
        self._task(quantity=2, worker=self.worker)
        self._task(quantity=2, worker=self.worker_b)

        response = self._get({'worker': str(self.worker_b.pk)})

        self.assertEqual(response.status_code, 200)
        # فقط ردیف‌های کارگر ب: دو ماده × ۲
        self.assertEqual(response.context['summary']['total_planned'], Decimal('4.00'))
        self.assertEqual(
            {p['queue'].worker_id for p in response.context['problems']},
            {self.worker_b.pk},
        )

    def test_25_material_filter_narrows_the_day(self):
        self._task(quantity=2)

        response = self._get({'material': str(self.raw.pk)})
        self.assertEqual(response.context['summary']['total_planned'], Decimal('2.00'))

        response = self._get({'material': str(self.raw_b.pk)})
        self.assertEqual(response.context['summary']['total_planned'], Decimal('2.00'))

    def test_26_totals_are_rendered(self):
        self._task(quantity=2)
        for queue in self._day_queues():
            self._deliver(queue)
        self._return(self._day_queues()[0], '0.50')

        response = self._get()
        self.assertContains(response, '4.00')   # مجموع برنامه و تحویل
        self.assertContains(response, '0.50')   # مجموع برگشت

    def test_27_navigation_link_exists(self):
        response = self._get()
        self.assertContains(response, reverse('inventory:daily_closing'))

    def test_28_closing_is_reachable_from_daily_queue_page(self):
        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))
        self.assertContains(response, reverse('inventory:daily_closing'))


class ClosingPermissionTests(ClosingBase):
    def test_29_warehouse_user_can_view(self):
        self._task(quantity=2)
        response = self._get(user=self.warehouse_user)
        self.assertEqual(response.status_code, 200)

    def test_30_superuser_can_view(self):
        self._task(quantity=2)
        response = self._get(user=self.superuser)
        self.assertEqual(response.status_code, 200)

    def test_31_unprivileged_user_is_redirected(self):
        self._task(quantity=2)
        response = self._get(user=self.plain_user)
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.url, reverse('inventory:daily_closing'))

    def test_32_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse('inventory:daily_closing'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.url)
