"""
Phase 9 تا 16 — Closing Confirmation و گزارش‌های فقط‌خواندنی.

این تست‌ها قوانین صریح تأیید روز را بررسی می‌کنند و یک smoke test برای
رندر شدن صفحه‌های جدید و سرویس‌های گزارش فراهم می‌کنند.
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

import jdatetime

from inventory import reports, services
from inventory.models import (
    DailyMaterialClosing,
    DailyMaterialQueue,
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


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('p9admin', password='pw')
        cls.warehouse = User.objects.create_user('p9wh', password='pw')
        group, _ = Group.objects.get_or_create(name='انبار')
        cls.warehouse.groups.add(group)
        cls.plain = User.objects.create_user('p9plain', password='pw')
        cls.worker = User.objects.create_user('p9w', password='pw', first_name='حسین')

        cls.raw_cat = RawMaterialCategory.objects.create(name='مواد')
        cls.raw = RawMaterial.objects.create(
            category=cls.raw_cat, name='رنگ سفید', code='P9-W', unit='kg',
            pack_size=Decimal('0'), min_stock_alert=Decimal('5'),
        )
        cls.raw2 = RawMaterial.objects.create(
            category=cls.raw_cat, name='رنگ کیلر', code='P9-K', unit='kg',
            pack_size=Decimal('0'), min_stock_alert=Decimal('0'),
        )
        cls.pcat = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(
            category=cls.pcat, name='محصول', base_price=100, is_active=True)
        cls.process = PaintingProcess.objects.create(
            name='نقاشی', code='P9', color_codes=['8'], is_active=True)
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='زیرکار',
            duration_minutes=30, drying_time_minutes=0)
        cls.customer = Customer.objects.create(name='مشتری', phone='0912')
        cls.order = Order.objects.create(
            user=cls.superuser, customer=cls.customer, number='P9-1')
        cls.item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.item)

        for raw in (cls.raw, cls.raw2):
            PaintingMaterialRequirement.objects.create(
                process=cls.process, raw_material=raw, product=cls.product,
                color_part='بدنه', consumption_per_unit=Decimal('1.000'))
            StockMovement.objects.create(
                raw_material=raw, movement_type='purchase',
                quantity=Decimal('50'), created_by=cls.superuser)

        cls.today = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.today, time(8, 0)))
        cls.jalali = jdatetime.date.today().strftime('%Y-%m-%d')

    def _task(self, **kwargs):
        return ProductionTask.objects.create(
            order=self.order, order_item=self.item, station_name='paint',
            step_order=1, quantity=kwargs.get('quantity', 2), status='pending',
            painting_stage=self.stage, color_part='بدنه',
            assigned_worker=kwargs.get('worker', self.worker),
            scheduled_start=kwargs.get('start', self.day_start), scheduled_end=None)

    def _rows(self, **kwargs):
        return list(DailyMaterialQueue.objects.filter(
            work_date=kwargs.get('date', self.today)).order_by('raw_material_id'))

    def _settle(self):
        for queue in self._rows():
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser)
            queue.refresh_from_db()
            services.execute_daily_return(
                queue_id=queue.pk, returned_by=self.superuser,
                returned_quantity=queue.delivered_quantity)

    def _get(self, name, user=None, params=None, args=None):
        self.client.force_login(user or self.warehouse)
        return self.client.get(reverse(f'inventory:{name}', args=args), params or {})


class ClosingConfirmationTests(Base):
    def test_01_healthy_day_can_be_confirmed(self):
        self._task()
        self._settle()

        closing = services.confirm_daily_closing(
            date=self.today, closed_by=self.warehouse, note='ok')

        self.assertEqual(closing.work_date, self.today)
        self.assertEqual(closing.status, 'confirmed')
        self.assertEqual(closing.closed_by, self.warehouse)
        self.assertEqual(closing.note, 'ok')

    def test_02_problem_day_cannot_be_confirmed(self):
        self._task()  # pending ⇒ incomplete
        with self.assertRaises(HandoverError):
            services.confirm_daily_closing(
                date=self.today, closed_by=self.warehouse)

    def test_03_conflict_day_cannot_be_confirmed(self):
        task = self._task()
        self._settle()
        task.quantity = 30
        task.save()

        with self.assertRaises(HandoverError):
            services.confirm_daily_closing(
                date=self.today, closed_by=self.warehouse)

    def test_04_day_cannot_be_closed_twice(self):
        self._task()
        self._settle()
        services.confirm_daily_closing(date=self.today, closed_by=self.warehouse)

        with self.assertRaises(HandoverError):
            services.confirm_daily_closing(
                date=self.today, closed_by=self.superuser)
        self.assertEqual(
            DailyMaterialClosing.objects.filter(work_date=self.today).count(), 1)

    def test_05_confirm_creates_no_stock_movement(self):
        self._task()
        self._settle()
        before = list(StockMovement.objects.values_list('id', flat=True))

        services.confirm_daily_closing(date=self.today, closed_by=self.warehouse)

        self.assertEqual(
            list(StockMovement.objects.values_list('id', flat=True)), before)

    def test_06_confirm_does_not_change_queue(self):
        self._task()
        self._settle()
        before = {
            q.pk: (q.planned_quantity, q.delivered_quantity, q.returned_quantity,
                   q.actual_consumption, q.status, q.updated_at)
            for q in self._rows()
        }

        services.confirm_daily_closing(date=self.today, closed_by=self.warehouse)

        for queue in self._rows():
            self.assertEqual(
                (queue.planned_quantity, queue.delivered_quantity,
                 queue.returned_quantity, queue.actual_consumption,
                 queue.status, queue.updated_at),
                before[queue.pk])

    def test_07_get_daily_closing_helper(self):
        self.assertIsNone(services.get_daily_closing(self.today))
        self._task()
        self._settle()
        services.confirm_daily_closing(date=self.today, closed_by=self.warehouse)
        self.assertIsNotNone(services.get_daily_closing(self.today))

    def test_08_confirm_via_post(self):
        self._task()
        self._settle()
        self.client.force_login(self.warehouse)
        response = self.client.post(
            reverse('inventory:daily_closing_confirm'),
            {'date': self.jalali, 'note': 'تأیید'})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            DailyMaterialClosing.objects.filter(work_date=self.today).exists())

    def test_09_confirm_post_rejects_problem_day(self):
        self._task()
        self.client.force_login(self.warehouse)
        response = self.client.post(
            reverse('inventory:daily_closing_confirm'), {'date': self.jalali},
            follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            DailyMaterialClosing.objects.filter(work_date=self.today).exists())

    def test_10_confirm_requires_permission(self):
        self._task()
        self._settle()
        self.client.force_login(self.plain)
        response = self.client.post(
            reverse('inventory:daily_closing_confirm'), {'date': self.jalali})
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(
            DailyMaterialClosing.objects.filter(work_date=self.today).exists())

    def test_11_closing_page_shows_confirm_form(self):
        self._task()
        self._settle()
        response = self._get('daily_closing')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'روز بررسی و تأیید شد')
        self.assertFalse(response.context['already_closed'])

    def test_12_closing_page_shows_closed_state(self):
        self._task()
        self._settle()
        services.confirm_daily_closing(date=self.today, closed_by=self.warehouse)
        response = self._get('daily_closing')
        self.assertTrue(response.context['already_closed'])
        self.assertContains(response, 'این روز بسته شده است')


class ReportServiceTests(Base):
    def test_13_ledger_lists_movements_with_balance(self):
        self._task()
        data = reports.material_ledger()

        self.assertGreaterEqual(data['total_count'], 2)  # دو خرید
        purchase_lines = [
            line for line in data['rows']
            if line['movement'].movement_type == 'purchase'
        ]
        self.assertTrue(purchase_lines)
        # موجودی آخر باید برابر مجموع خریدها باشد
        last_by_material = {}
        for line in data['rows']:
            last_by_material[line['movement'].raw_material_id] = line['balance']
        self.assertEqual(last_by_material[self.raw.pk], Decimal('50.00'))

    def test_14_ledger_consumption_reduces_balance(self):
        self._task()
        queue = DailyMaterialQueue.objects.filter(
            work_date=self.today, raw_material=self.raw).first()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        data = reports.material_ledger()
        lines = [line for line in data['rows']
                 if line['movement'].movement_type == 'consumption']
        self.assertTrue(lines)
        self.assertLess(lines[-1]['balance'], Decimal('50.00'))

    def test_15_ledger_filters_by_material(self):
        self._task()
        data = reports.material_ledger(material_id=self.raw.pk)
        self.assertTrue(data['rows'])
        for line in data['rows']:
            self.assertEqual(line['movement'].raw_material_id, self.raw.pk)

    def test_16_ledger_is_read_only(self):
        self._task()
        before = list(StockMovement.objects.values_list('id', flat=True))
        reports.material_ledger()
        self.assertEqual(list(StockMovement.objects.values_list('id', flat=True)), before)

    def test_17_consumption_report_uses_stored_values(self):
        self._task()
        queue = DailyMaterialQueue.objects.filter(
            work_date=self.today, raw_material=self.raw).first()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        data = reports.consumption_report(date=self.today)
        row = next(r for r in data['rows'] if r['raw_material_id'] == self.raw.pk)

        self.assertEqual(row['planned'], Decimal('2.00'))
        self.assertEqual(row['delivered'], Decimal('2.00'))
        self.assertEqual(row['actual'], Decimal('2.00'))
        self.assertEqual(row['variance'], Decimal('0.00'))

    def test_18_consumption_report_variance_is_numeric_difference(self):
        self._task()
        queue = DailyMaterialQueue.objects.filter(
            work_date=self.today, raw_material=self.raw).first()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.50')

        data = reports.consumption_report(date=self.today)
        row = next(r for r in data['rows'] if r['raw_material_id'] == self.raw.pk)
        self.assertEqual(row['actual'], Decimal('1.50'))
        self.assertEqual(row['variance'], Decimal('-0.50'))

    def test_19_order_traceability(self):
        self._task()
        data = reports.order_material_traceability(self.order)

        self.assertTrue(data['rows'])
        row = next(r for r in data['rows'] if r['raw_material_id'] == self.raw.pk)
        self.assertEqual(row['planned'], Decimal('2.00'))
        self.assertGreaterEqual(row['task_count'], 1)
        self.assertIsNotNone(data['totals'])

    def test_20_order_traceability_without_tasks(self):
        data = reports.order_material_traceability(self.order)
        self.assertEqual(data['rows'], [])

    def test_21_dashboard_sections(self):
        self._task()
        data = reports.material_planning_dashboard(self.today)

        self.assertIn('manager', data)
        self.assertIn('warehouse', data)
        self.assertGreaterEqual(data['manager']['painting_tasks'], 1)
        self.assertGreaterEqual(data['manager']['planned'], Decimal('4.00'))
        self.assertTrue(data['warehouse']['materials'])
        self.assertTrue(data['warehouse']['workers'])

    def test_22_dashboard_flags_low_stock(self):
        self._task()
        data = reports.material_planning_dashboard(self.today)
        low = [m for m in data['warehouse']['materials'] if m['low']]
        # موجودی ۵۰ و حد هشدار ۵ ⇒ کم‌موجود نیست
        self.assertEqual(low, [])

    def test_23_alerts_include_incomplete_and_closing_required(self):
        self._task()
        data = reports.inventory_alerts(self.today)
        kinds = {alert['kind'] for alert in data['alerts']}
        self.assertIn('incomplete', kinds)
        self.assertIn('closing_required', kinds)

    def test_24_alerts_include_unreturned(self):
        self._task()
        for queue in self._rows():
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser)
        data = reports.inventory_alerts(self.today)
        kinds = {alert['kind'] for alert in data['alerts']}
        self.assertIn('unreturned', kinds)

    def test_25_alerts_low_stock(self):
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='consumption',
            quantity=Decimal('49'), created_by=self.superuser)
        data = reports.inventory_alerts(self.today)
        kinds = {alert['kind'] for alert in data['alerts']}
        self.assertIn('low_stock', kinds)

    def test_26_alerts_disappear_after_closing(self):
        self._task()
        self._settle()
        services.confirm_daily_closing(date=self.today, closed_by=self.warehouse)
        data = reports.inventory_alerts(self.today)
        kinds = {alert['kind'] for alert in data['alerts']}
        self.assertNotIn('closing_required', kinds)
        self.assertNotIn('incomplete', kinds)

    def test_27_alerts_create_nothing(self):
        self._task()
        before = list(StockMovement.objects.values_list('id', flat=True))
        queue_state = list(
            DailyMaterialQueue.objects.values_list('id', 'delivered_quantity'))
        reports.inventory_alerts(self.today)
        self.assertEqual(list(StockMovement.objects.values_list('id', flat=True)), before)
        self.assertEqual(
            list(DailyMaterialQueue.objects.values_list('id', 'delivered_quantity')),
            queue_state)

    def test_28_historical_report_types(self):
        self._task()
        for key, _label in reports.HISTORICAL_REPORTS:
            data = reports.historical_report(report=key)
            self.assertIn('columns', data)
            self.assertIn('rows', data)

    def test_29_historical_report_rejects_unknown(self):
        with self.assertRaises(ValueError):
            reports.historical_report(report='nope')

    def test_30_historical_closing_report(self):
        self._task()
        self._settle()
        services.confirm_daily_closing(date=self.today, closed_by=self.warehouse)
        data = reports.historical_report(report='closing')
        self.assertTrue(data['rows'])

    def test_31_audit_detects_returned_greater_than_delivered(self):
        self._task()
        queue = DailyMaterialQueue.objects.filter(
            work_date=self.today, raw_material=self.raw).first()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()
        queue.returned_quantity = queue.delivered_quantity + Decimal('5')
        queue.save(update_fields=['returned_quantity'])

        data = reports.data_integrity_audit()
        problems = {issue['problem'] for issue in data['issues']}
        self.assertIn('returned_gt_delivered', problems)
        self.assertIn('actual_mismatch', problems)

    def test_32_audit_detects_actual_mismatch(self):
        self._task()
        queue = DailyMaterialQueue.objects.filter(
            work_date=self.today, raw_material=self.raw).first()
        queue.actual_consumption = Decimal('9.99')
        queue.save(update_fields=['actual_consumption'])

        data = reports.data_integrity_audit()
        self.assertIn('actual_mismatch', {i['problem'] for i in data['issues']})

    def test_33_audit_detects_cancelled_with_transaction(self):
        self._task()
        queue = DailyMaterialQueue.objects.filter(
            work_date=self.today, raw_material=self.raw).first()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()
        queue.status = 'cancelled'
        queue.save(update_fields=['status'])

        data = reports.data_integrity_audit()
        self.assertIn(
            'cancelled_with_transaction', {i['problem'] for i in data['issues']})

    def test_34_audit_detects_negative_actual(self):
        self._task()
        queue = DailyMaterialQueue.objects.filter(
            work_date=self.today, raw_material=self.raw).first()
        queue.delivered_quantity = Decimal('1')
        queue.returned_quantity = Decimal('3')
        queue.actual_consumption = Decimal('-2')
        queue.save(update_fields=['delivered_quantity', 'returned_quantity',
                                  'actual_consumption'])

        data = reports.data_integrity_audit()
        self.assertIn('negative_actual', {i['problem'] for i in data['issues']})

    def test_35_audit_is_clean_for_healthy_data(self):
        self._task()
        self._settle()
        data = reports.data_integrity_audit()
        self.assertEqual(
            [i for i in data['issues'] if i['severity'] == 'critical'], [])

    def test_36_audit_does_not_modify_data(self):
        self._task()
        before = list(
            DailyMaterialQueue.objects.values_list(
                'id', 'planned_quantity', 'delivered_quantity',
                'returned_quantity', 'actual_consumption', 'status'))
        reports.data_integrity_audit()
        self.assertEqual(
            list(DailyMaterialQueue.objects.values_list(
                'id', 'planned_quantity', 'delivered_quantity',
                'returned_quantity', 'actual_consumption', 'status')),
            before)


class NewPageRenderTests(Base):
    def test_37_ledger_page(self):
        self._task()
        response = self._get('material_ledger')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/material_ledger.html')

    def test_38_consumption_page(self):
        self._task()
        response = self._get('consumption_report')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/consumption_report.html')

    def test_39_dashboard_page(self):
        self._task()
        response = self._get('material_dashboard', params={'date': self.jalali})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/material_dashboard.html')

    def test_40_alerts_page(self):
        self._task()
        response = self._get('inventory_alerts', params={'date': self.jalali})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/inventory_alerts.html')

    def test_41_historical_page(self):
        self._task()
        response = self._get('historical_reports')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/historical_reports.html')

    def test_42_historical_csv_export(self):
        self._task()
        response = self._get('historical_reports_csv')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])

    def test_43_audit_page(self):
        self._task()
        response = self._get('data_integrity_audit', user=self.superuser)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/data_integrity_audit.html')

    def test_44_audit_page_requires_manager(self):
        self._task()
        response = self._get('data_integrity_audit', user=self.plain)
        self.assertNotEqual(response.status_code, 200)

    def test_45_order_trace_page(self):
        self._task()
        response = self._get('order_material_traceability', args=[self.order.pk])
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/order_material_trace.html')

    def test_46_report_pages_require_permission(self):
        self._task()
        for name, args in (
            ('material_ledger', None),
            ('consumption_report', None),
            ('material_dashboard', None),
            ('inventory_alerts', None),
            ('historical_reports', None),
        ):
            response = self._get(name, user=self.plain, args=args)
            self.assertNotEqual(response.status_code, 200, name)

    def test_47_navigation_contains_new_pages(self):
        response = self._get('daily_material_queue')
        for name in ('material_ledger', 'consumption_report', 'material_dashboard',
                     'inventory_alerts', 'historical_reports',
                     'data_integrity_audit', 'daily_closing'):
            self.assertContains(response, reverse(f'inventory:{name}', args=[1])
                                if name == 'order_material_traceability'
                                else reverse(f'inventory:{name}'))
