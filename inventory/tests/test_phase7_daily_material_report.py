"""
Phase 7 — گزارش مصرف مواد روزانه (Consumption & Warehouse Reporting).

این گزارش فقط می‌خواند. منبع دادهٔ آن ``DailyMaterialQueue`` است و مصرف واقعی
از فیلد ``actual_consumption`` خودِ ردیف صف خوانده می‌شود، نه از محاسبهٔ موازی.
منبع حقیقت موجودی انبار همچنان ``StockMovement`` است.
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


class ReportBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('p7admin', password='pw')
        cls.warehouse_user = User.objects.create_user('p7warehouse', password='pw')
        group, _ = Group.objects.get_or_create(name='انبار')
        cls.warehouse_user.groups.add(group)
        cls.plain_user = User.objects.create_user('p7plain', password='pw')

        cls.worker_a = User.objects.create_user('p7wa', password='pw', first_name='حسین')
        cls.worker_b = User.objects.create_user('p7wb', password='pw', first_name='علی')

        cls.raw_category = RawMaterialCategory.objects.create(name='مواد نقاشی')
        cls.raw = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ سفید', code='R-WHITE',
            unit='kg', pack_size=Decimal('0'),
        )
        cls.raw2 = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ کیلر', code='R-KILLER',
            unit='kg', pack_size=Decimal('0'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته تست')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول تست', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند نقاشی', code='P7', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='زیرکار',
            duration_minutes=30, drying_time_minutes=0,
        )
        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.superuser, customer=cls.customer, number='P7-1',
        )

        cls.item_a = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.item_a)
        cls.item_b = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.item_b)

        # فقط «رنگ سفید» نیاز برنامه‌ریزی دارد؛ هر تسک یک ردیف صف می‌سازد.
        # «رنگ کیلر» فقط برای تست فیلتر ماده و صف بدون منبع استفاده می‌شود.
        PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.raw, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.500'),
        )

        for raw in (cls.raw, cls.raw2):
            StockMovement.objects.create(
                raw_material=raw, movement_type='purchase',
                quantity=Decimal('100'), created_by=cls.superuser,
            )

        cls.today = timezone.localdate()
        cls.day_start = timezone.make_aware(timezone.datetime.combine(cls.today, time(8, 0)))

    def _make_task(self, *, worker, order_item, start=None, quantity=2):
        return ProductionTask.objects.create(
            order=self.order,
            order_item=order_item,
            station_name='paint',
            step_order=1,
            quantity=quantity,
            status='pending',
            painting_stage=self.stage,
            color_part='بدنه',
            assigned_worker=worker,
            scheduled_start=start or self.day_start,
            scheduled_end=None,
        )

    def _queue(self, *, worker, raw, date=None):
        return DailyMaterialQueue.objects.filter(
            work_date=date or self.today, worker=worker, raw_material=raw,
        ).first()

    def _report(self, **kwargs):
        return services.daily_material_report(self.today, **kwargs)

    def _get_page(self, params=None, user=None):
        self.client.force_login(user or self.warehouse_user)
        return self.client.get(
            reverse('inventory:daily_material_queue'), params or {},
        )


class ReportRowTests(ReportBase):
    def test_01_planned_quantity_is_shown(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        report = self._report()

        rows = list(report['rows'])
        self.assertEqual(len(rows), 1)
        # ۴ واحد × ۰٫۵ = ۲ کیلو
        self.assertEqual(rows[0].planned_quantity, Decimal('2.00'))
        self.assertEqual(report['summary']['total_planned'], Decimal('2.00'))

        response = self._get_page()
        self.assertContains(response, '2.00')

    def test_02_delivered_quantity_is_shown(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        report = self._report()
        row = list(report['rows'])[0]
        self.assertEqual(row.delivered_quantity, Decimal('2.00'))
        self.assertEqual(row.status, 'delivered')
        self.assertEqual(report['summary']['total_delivered'], Decimal('2.00'))

        response = self._get_page()
        self.assertContains(response, '2.00')

    def test_03_returned_quantity_is_shown(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.50',
        )

        report = self._report()
        self.assertEqual(report['summary']['total_returned'], Decimal('0.50'))

        response = self._get_page()
        self.assertContains(response, '0.50')

    def test_04_actual_consumption_uses_queue_logic(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.75',
        )

        report = self._report()
        row = list(report['rows'])[0]

        # ۲٫۰۰ − ۰٫۷۵ = ۱٫۲۵ ؛ همان فیلد ذخیره‌شدهٔ صف
        self.assertEqual(row.actual_consumption, Decimal('1.25'))
        self.assertEqual(row.actual_consumption, row.computed_actual_consumption)
        self.assertEqual(report['summary']['total_actual'], Decimal('1.25'))

    def test_05_returned_is_not_counted_as_consumption(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.50',
        )

        report = self._report()
        summary = report['summary']

        self.assertEqual(summary['total_returned'], Decimal('0.50'))
        self.assertNotEqual(summary['total_actual'], summary['total_returned'])
        # مصرف واقعی = delivered − returned
        self.assertEqual(
            summary['total_actual'],
            summary['total_delivered'] - summary['total_returned'],
        )

    def test_06_data_without_transaction(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        report = self._report()

        row = list(report['rows'])[0]
        self.assertEqual(row.status, 'pending')
        self.assertEqual(row.delivered_quantity, Decimal('0.00'))
        self.assertEqual(row.returned_quantity, Decimal('0.00'))
        self.assertEqual(row.actual_consumption, Decimal('0.00'))
        self.assertFalse(row.has_transaction)

        summary = report['summary']
        self.assertEqual(summary['total_planned'], Decimal('2.00'))
        self.assertEqual(summary['total_actual'], Decimal('0.00'))
        self.assertEqual(summary['pending_count'], 1)

    def test_07_data_after_delivery(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        summary = self._report()['summary']
        self.assertEqual(summary['delivered_count'], 1)
        self.assertEqual(summary['pending_count'], 0)
        self.assertEqual(summary['total_delivered'], Decimal('2.00'))
        self.assertEqual(summary['total_actual'], Decimal('2.00'))

    def test_08_data_after_partial_return(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.50',
        )

        report = self._report()
        row = list(report['rows'])[0]
        self.assertEqual(row.status, 'delivered')
        self.assertEqual(row.returned_quantity, Decimal('0.50'))
        self.assertEqual(row.actual_consumption, Decimal('1.50'))

    def test_09_data_after_full_return(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='2.00',
        )

        report = self._report()
        row = list(report['rows'])[0]
        self.assertEqual(row.status, 'returned')
        self.assertEqual(row.returned_quantity, Decimal('2.00'))
        self.assertEqual(row.actual_consumption, Decimal('0.00'))
        self.assertEqual(report['summary']['returned_count'], 1)

    def test_10_planned_quantity_never_changes_by_report(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.50',
        )

        planned_before = queue.planned_quantity
        sources_before = queue.sources.count()

        for _ in range(3):
            self._report()

        queue.refresh_from_db()
        self.assertEqual(queue.planned_quantity, planned_before)
        self.assertEqual(queue.sources.count(), sources_before)


class ReportSummaryTests(ReportBase):
    def _seed_two_rows(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        self._make_task(worker=self.worker_b, order_item=self.item_b, quantity=8)

    def test_11_daily_summary_aggregates_all_rows(self):
        self._seed_two_rows()
        qa = self._queue(worker=self.worker_a, raw=self.raw)
        qb = self._queue(worker=self.worker_b, raw=self.raw)
        services.execute_daily_delivery(queue_id=qa.pk, delivered_by=self.superuser)
        services.execute_daily_delivery(queue_id=qb.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=qb.pk, returned_by=self.superuser, returned_quantity='1.00',
        )

        summary = self._report()['summary']

        self.assertEqual(summary['items'], 2)
        self.assertEqual(summary['total_planned'], Decimal('6.00'))
        self.assertEqual(summary['total_delivered'], Decimal('6.00'))
        self.assertEqual(summary['total_returned'], Decimal('1.00'))
        self.assertEqual(summary['total_actual'], Decimal('5.00'))
        self.assertEqual(summary['delivered_count'], 2)

    def test_12_summary_is_visible_on_page(self):
        self._seed_two_rows()

        response = self._get_page()
        summary = response.context['summary']

        self.assertEqual(summary['items'], 2)
        self.assertEqual(summary['total_planned'], Decimal('6.00'))
        self.assertContains(response, '6.00')

    def test_13_summary_excludes_cancelled_rows_by_default(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        queue.status = 'cancelled'
        queue.save(update_fields=['status'])

        summary = self._report()['summary']
        self.assertEqual(summary['items'], 0)
        self.assertEqual(summary['total_planned'], Decimal('0.00'))

    def test_14_summary_can_include_cancelled(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        queue.status = 'cancelled'
        queue.save(update_fields=['status'])

        qs = services.daily_queue_filtered_queryset(
            self.today, include_cancelled=True,
        )
        summary = services.daily_queue_summary(qs)
        self.assertEqual(summary['items'], 1)
        self.assertEqual(summary['total_planned'], Decimal('2.00'))

    def test_15_empty_day_summary_is_zero_not_error(self):
        summary = self._report()['summary']
        self.assertEqual(summary['items'], 0)
        self.assertEqual(summary['total_planned'], Decimal('0.00'))
        self.assertEqual(summary['total_actual'], Decimal('0.00'))

        response = self._get_page()
        self.assertEqual(response.status_code, 200)


class ReportFilterTests(ReportBase):
    def _seed(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        self._make_task(worker=self.worker_b, order_item=self.item_b, quantity=8)

    def _queryset_for_date(self, date):
        return services.daily_queue_filtered_queryset(date)

    def test_16_date_filter(self):
        self._seed()
        tomorrow = self.today + timezone.timedelta(days=1)
        self._make_task(
            worker=self.worker_a, order_item=self.item_a,
            start=timezone.make_aware(
                timezone.datetime.combine(tomorrow, time(8, 0))
            ),
        )

        today_report = self._report()
        tomorrow_report = services.daily_material_report(tomorrow)

        self.assertEqual(len(list(today_report['rows'])), 2)
        self.assertEqual(len(list(tomorrow_report['rows'])), 1)

        # صفحه هم باید بتواند روز را انتخاب کند
        tomorrow_jalali = jdatetime.date.fromgregorian(date=tomorrow)
        response = self._get_page({
            'date': tomorrow_jalali.strftime('%Y-%m-%d'),
        })
        self.assertEqual(response.context['selected_gregorian'], tomorrow)
        self.assertEqual(len(response.context['queues']), 1)

    def test_17_date_filter_is_primary_and_defaults_to_today(self):
        self._seed()
        response = self._get_page()
        self.assertEqual(response.context['selected_gregorian'], self.today)

    def test_18_worker_filter(self):
        self._seed()
        report = self._report(worker_id=self.worker_b.pk)

        rows = list(report['rows'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].worker, self.worker_b)
        self.assertEqual(report['summary']['items'], 1)
        self.assertEqual(report['summary']['total_planned'], Decimal('4.00'))

    def test_19_worker_filter_on_page(self):
        self._seed()
        response = self._get_page({'worker': str(self.worker_a.pk)})
        self.assertEqual(len(response.context['queues']), 1)

    def test_20_material_filter(self):
        self._seed()
        report = self._report(material_id=self.raw2.pk)

        self.assertEqual(len(list(report['rows'])), 0)

    def test_21_material_filter_on_page(self):
        self._seed()
        response = self._get_page({'material': str(self.raw.pk)})
        self.assertEqual(len(response.context['queues']), 2)

        response = self._get_page({'material': str(self.raw2.pk)})
        self.assertEqual(len(response.context['queues']), 0)

    def test_22_status_filter(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        delivered = self._report(status='delivered')
        pending = self._report(status='pending')

        self.assertEqual(len(list(delivered['rows'])), 1)
        self.assertEqual(len(list(pending['rows'])), 0)

    def test_23_status_filter_on_page(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        response = self._get_page({'status': 'delivered'})
        self.assertEqual(len(response.context['queues']), 1)

        response = self._get_page({'status': 'pending'})
        self.assertEqual(len(response.context['queues']), 0)

    def test_24_combined_filters(self):
        self._seed()
        report = self._report(
            worker_id=self.worker_a.pk, material_id=self.raw.pk, status='pending',
        )
        self.assertEqual(len(list(report['rows'])), 1)

    def test_25_invalid_filter_values_are_ignored(self):
        self._seed()
        response = self._get_page({
            'worker': 'abc', 'material': 'xyz', 'status': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['queues']), 2)


class ReportConflictTests(ReportBase):
    def test_26_conflict_is_counted_and_visible(self):
        task = self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        # تغییر برنامه پس از تحویل → تعارض
        task.quantity = 20
        task.save()
        queue.refresh_from_db()

        self.assertTrue(queue.has_plan_conflict)
        self.assertTrue(queue.conflict_note)

        summary = self._report()['summary']
        self.assertEqual(summary['conflict_count'], 1)

        response = self._get_page()
        self.assertContains(response, 'مغایرت برنامه')
        self.assertContains(response, 'مغایرت')

    def test_27_no_conflict_when_plan_matches(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        self._make_task(worker=self.worker_b, order_item=self.item_b, quantity=4)

        summary = self._report()['summary']
        self.assertEqual(summary['conflict_count'], 0)

    def test_28_conflict_row_keeps_real_history(self):
        task = self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        task.quantity = 20
        task.save()

        row = list(self._report()['rows'])[0]
        self.assertEqual(row.planned_quantity, Decimal('2.00'))
        self.assertEqual(row.delivered_quantity, Decimal('2.00'))
        self.assertEqual(row.actual_consumption, Decimal('2.00'))
        self.assertTrue(row.has_plan_conflict)


class ReportReadOnlyTests(ReportBase):
    def test_29_report_creates_no_stock_movement(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.50',
        )

        movements_before = list(
            StockMovement.objects.values_list('id', 'movement_type', 'quantity')
        )

        for _ in range(3):
            self._report()
        self._get_page()

        self.assertEqual(
            list(StockMovement.objects.values_list('id', 'movement_type', 'quantity')),
            movements_before,
            'گزارش نباید StockMovement بسازد.',
        )

    def test_30_report_does_not_change_queue_rows(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()

        snapshot = (
            queue.planned_quantity, queue.delivered_quantity,
            queue.returned_quantity, queue.actual_consumption,
            queue.status, queue.has_plan_conflict, queue.conflict_note,
        )
        sources_before = list(
            queue.sources.values_list('production_task_id', 'quantity')
        )

        self._report()
        self._report()

        queue.refresh_from_db()
        self.assertEqual(
            (
                queue.planned_quantity, queue.delivered_quantity,
                queue.returned_quantity, queue.actual_consumption,
                queue.status, queue.has_plan_conflict, queue.conflict_note,
            ),
            snapshot,
        )
        self.assertEqual(
            list(queue.sources.values_list('production_task_id', 'quantity')),
            sources_before,
        )

    def test_31_summary_uses_stored_actual_consumption_not_recalculation(self):
        """اگر فیلد ذخیره‌شده عمداً متفاوت باشد، گزارش همان را نشان می‌دهد."""
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = self._queue(worker=self.worker_a, raw=self.raw)
        queue.actual_consumption = Decimal('7.77')
        queue.save(update_fields=['actual_consumption'])

        summary = self._report()['summary']
        self.assertEqual(summary['total_actual'], Decimal('7.77'))

    def test_32_report_handles_queue_without_sources(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        queue = DailyMaterialQueue.objects.create(
            work_date=self.today, worker=self.worker_b, raw_material=self.raw2,
            planned_quantity=Decimal('3.00'), status='pending',
        )
        self.assertEqual(queue.sources.count(), 0)

        rows = list(self._report()['rows'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(self._report()['summary']['total_planned'], Decimal('5.00'))


class ReportPermissionTests(ReportBase):
    def test_33_superuser_can_view_report(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        response = self._get_page(user=self.superuser)
        self.assertEqual(response.status_code, 200)

    def test_34_warehouse_user_can_view_report(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        response = self._get_page(user=self.warehouse_user)
        self.assertEqual(response.status_code, 200)

    def test_35_unprivileged_user_is_redirected(self):
        self._make_task(worker=self.worker_a, order_item=self.item_a, quantity=4)
        response = self._get_page(user=self.plain_user)
        self.assertEqual(response.status_code, 302)

    def test_36_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse('inventory:daily_material_queue'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.url)


class ReportQueryCountTests(ReportBase):
    def test_37_report_summary_is_aggregated_not_row_by_row(self):
        """جمع روزانه باید با aggregate بیاید، نه با جمع در حلقه."""
        for worker, item in ((self.worker_a, self.item_a), (self.worker_b, self.item_b)):
            self._make_task(worker=worker, order_item=item, quantity=4)

        with self.assertNumQueries(1):
            # 1 aggregate — شمارش وضعیت‌ها و تعارض‌ها همگی در همان یک کوئری‌اند.
            services.daily_queue_summary(
                services.daily_queue_filtered_queryset(self.today)
            )

    def test_38_page_query_count_does_not_grow_with_rows(self):
        """افزودن ردیف نباید باعث رشد کوئری‌ها شود (ضد N+1)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def page_queries():
            with CaptureQueriesContext(connection) as captured:
                response = self._get_page()
            self.assertEqual(response.status_code, 200)
            return len(captured)

        base = page_queries()  # بدون هیچ ردیفی

        for worker, item in ((self.worker_a, self.item_a), (self.worker_b, self.item_b)):
            self._make_task(worker=worker, order_item=item, quantity=4)

        with_rows = page_queries()

        # فقط «شمارش صفحه‌بندی» و «خواندن ردیف‌ها» اضافه می‌شوند.
        self.assertLessEqual(
            with_rows - base, 4,
            f'احتمالاً N+1 query در گزارش وجود دارد: {base} → {with_rows}',
        )