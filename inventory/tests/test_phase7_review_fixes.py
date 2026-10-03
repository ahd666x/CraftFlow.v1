"""
Phase 7.1 — اصلاحات ناشی از local review.

هر تست به یک ایراد تأییدشده اشاره دارد:

* A) ردیف صف باید دقیقاً همان مقداری را ثبت کند که ``StockMovement`` از انبار
  کم می‌کند (پک گردشده)، تا گزارش، مصرف واقعی و حداکثر قابل برگشت درست باشند.
* B) فیلتر تاریخ باید همان روزی را نشان دهد که کاربر انتخاب کرده است.
* C) داده‌های متنی کاربر (مثل ``color_part``) نباید به‌عنوان HTML وارد DOM شوند.
* D) فیلتر «لغو شده» باید ردیف‌های لغوشده را برگرداند.
* E) انتقال تسک از ایستگاه نقاشی، صف روز قبل را هم sync کند.
* F) جمع با دقت کامل انجام شود و نیاز کوچک حذف نشود.
* G) وضعیت و حدود اقدام از سرویس بیاید، نه از بازخوانی در JS.
* H) پارامتر بی‌اثر ``dry_run`` حذف شده باشد.
* I) جمع روزانه با یک aggregate انجام شود.
* J) قانون سقف بسته‌بندی یکی و اعشاری باشد.
"""
from datetime import time
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
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

TEMPLATE = Path('inventory/templates/inventory/daily_material_queue.html')


class ReviewBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('r7admin', password='pw')
        cls.warehouse_user = User.objects.create_user('r7warehouse', password='pw')
        group, _ = Group.objects.get_or_create(name='انبار')
        cls.warehouse_user.groups.add(group)

        cls.worker = User.objects.create_user('r7worker', password='pw', first_name='حسین')

        cls.raw_category = RawMaterialCategory.objects.create(name='مواد نقاشی')
        # بدون بسته‌بندی
        cls.raw = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ سفید', code='R-WHITE',
            unit='kg', pack_size=Decimal('0'),
        )
        # بستهٔ ۴ کیلویی — برای آزمون reconciling
        cls.raw_pack4 = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ کیلر', code='R-KILLER',
            unit='kg', pack_size=Decimal('4'),
        )
        # بستهٔ ریز — برای آزمون سقف اعشاری
        cls.raw_tiny = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ خام', code='R-TINY',
            unit='kg', pack_size=Decimal('0.01'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته تست')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول تست', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند نقاشی', code='R7', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='زیرکار',
            duration_minutes=30, drying_time_minutes=0,
        )
        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.superuser, customer=cls.customer, number='R7-1',
        )
        cls.order_item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=2,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)

        # planned = 6 × 0.5 = 3 kg  →  با پک ۴ باید ۴ کیلو تحویل شود
        PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.raw_pack4, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.500'),
        )
        # نیاز بسیار کوچک برای آزمون دقت
        PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.raw_tiny, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.004'),
        )

        for raw, qty in ((cls.raw, '100'), (cls.raw_pack4, '50'), (cls.raw_tiny, '10')):
            StockMovement.objects.create(
                raw_material=raw, movement_type='purchase',
                quantity=Decimal(qty), created_by=cls.superuser,
            )

        cls.today = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.today, time(8, 0))
        )

    def _task(self, *, quantity=6, worker=None, start=None, station='paint',
              stage=None, item=None):
        return ProductionTask.objects.create(
            order=self.order,
            order_item=item or self.order_item,
            station_name=station,
            step_order=1,
            quantity=quantity,
            status='pending',
            painting_stage=stage or self.stage,
            color_part='بدنه',
            assigned_worker=worker or self.worker,
            scheduled_start=start or self.day_start,
            scheduled_end=None,
        )

    def _queue(self, raw=None, date=None, worker=None):
        return DailyMaterialQueue.objects.filter(
            work_date=date or self.today,
            worker=worker or self.worker,
            raw_material=raw or self.raw,
        ).first()

    def _stock(self, raw):
        return raw.current_stock


class DeliveryAccountingTests(ReviewBase):
    """A) ردیف صف باید با حرکت انبار reconcile شود."""

    def test_01_queue_records_physical_pack_quantity_not_planned_need(self):
        self._task(quantity=6)  # planned = 3
        queue = self._queue(raw=self.raw_pack4)
        self.assertEqual(queue.planned_quantity, Decimal('3.00'))

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()

        # ۳ نیاز، پک ۴ ⇒ مقدار فیزیکی ۴
        self.assertEqual(queue.delivered_quantity, Decimal('4.00'))

        movement = StockMovement.objects.filter(
            raw_material=self.raw_pack4, movement_type='consumption',
        ).order_by('-id').first()
        self.assertEqual(movement.quantity, Decimal('4.00'))

    def test_02_planned_quantity_never_changes_on_delivery(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        planned_before = queue.planned_quantity

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()

        self.assertEqual(queue.planned_quantity, planned_before)
        self.assertEqual(queue.planned_quantity, Decimal('3.00'))

    def test_03_queue_and_movement_reconcile(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        stock_before = self._stock(self.raw_pack4)

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()
        stock_after = self._stock(self.raw_pack4)

        moved = StockMovement.objects.filter(
            raw_material=self.raw_pack4, movement_type='consumption',
        ).order_by('-id').first().quantity

        # موجودی کم‌شده == مقدار ثبت‌شده در صف == مقدار حرکت انبار
        self.assertEqual(stock_before - stock_after, moved)
        self.assertEqual(stock_before - stock_after, queue.delivered_quantity)

    def test_04_actual_consumption_matches_delivered_after_full_delivery(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()

        self.assertEqual(queue.actual_consumption, Decimal('4.00'))
        # پک اضافه به‌صورت مصرف اضافه گزارش می‌شود
        self.assertEqual(queue.excess_consumption, Decimal('1.00'))

    def test_05_full_pack_can_be_returned_and_stock_is_fully_restored(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        stock_before = self._stock(self.raw_pack4)

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='4.00',
        )
        queue.refresh_from_db()

        self.assertEqual(queue.returned_quantity, Decimal('4.00'))
        self.assertEqual(queue.actual_consumption, Decimal('0.00'))
        self.assertEqual(queue.status, 'returned')
        # نه کیلو بدون حساب نمانده است
        self.assertEqual(self._stock(self.raw_pack4), stock_before)

    def test_06_surplus_pack_return_leaves_actual_equal_to_planned(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='1.00',
        )
        queue.refresh_from_db()

        self.assertEqual(queue.actual_consumption, Decimal('3.00'))
        self.assertEqual(queue.actual_consumption, queue.planned_quantity)
        self.assertEqual(queue.excess_consumption, Decimal('0.00'))

    def test_07_no_pack_size_delivers_planned_exactly(self):
        PaintingMaterialRequirement.objects.create(
            process=self.process, raw_material=self.raw, product=self.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.500'),
        )
        self._task(quantity=6)
        queue = self._queue(raw=self.raw)

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()

        self.assertEqual(queue.delivered_quantity, Decimal('3.00'))
        self.assertEqual(queue.actual_consumption, Decimal('3.00'))

    def test_08_preview_reports_physical_and_can_deliver(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)

        preview = services.preview_daily_delivery(queue.pk)
        self.assertEqual(preview['physical'], Decimal('4.00'))
        self.assertEqual(preview['packs'], 1)
        self.assertTrue(preview['can_deliver'])
        self.assertEqual(preview['status'], 'pending')

        # پس از تحویل دیگر قابل تحویل نیست
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        preview = services.preview_daily_delivery(queue.pk)
        self.assertFalse(preview['can_deliver'])
        self.assertEqual(preview['status'], 'delivered')

    def test_09_report_shows_physical_delivered_and_correct_actual(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='1.00',
        )

        report = services.daily_material_report(self.today)
        row = next(r for r in report['rows'] if r.raw_material_id == self.raw_pack4.pk)

        self.assertEqual(row.planned_quantity, Decimal('3.00'))
        self.assertEqual(row.delivered_quantity, Decimal('4.00'))
        self.assertEqual(row.returned_quantity, Decimal('1.00'))
        self.assertEqual(row.actual_consumption, Decimal('3.00'))
        self.assertEqual(report['summary']['total_delivered'], Decimal('4.00'))


class DateFilterTests(ReviewBase):
    """B) فیلتر تاریخ باید همان روز انتخابی را نشان دهد."""

    def test_10_chosen_jalali_date_is_displayed_and_shown(self):
        tomorrow = self.today + timezone.timedelta(days=1)
        start = timezone.make_aware(timezone.datetime.combine(tomorrow, time(8, 0)))
        self._task(quantity=6, start=start)
        tomorrow_jalali = jdatetime.date.fromgregorian(date=tomorrow)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(
            reverse('inventory:daily_material_queue'),
            {'date': tomorrow_jalali.strftime('%Y-%m-%d')},
        )

        self.assertEqual(response.status_code, 200)
        # روز انتخابی دقیقاً همان روزی است که کاربر خواسته
        self.assertEqual(response.context['selected_gregorian'], tomorrow)
        self.assertEqual(
            response.context['date_str'], tomorrow_jalali.strftime('%Y-%m-%d')
        )
        queues = list(response.context['queues'])
        self.assertTrue(queues)
        for queue in queues:
            self.assertEqual(
                queue.work_date, tomorrow,
                'همهٔ ردیف‌ها باید متعلق به روز انتخابی باشند.',
            )

    def test_11_gregorian_looking_date_is_not_silently_shifted(self):
        """تاریخ امروز به شمسی باید همان روز را برگرداند، نه روز دیگر."""
        today_jalali = jdatetime.date.today()

        self.client.force_login(self.warehouse_user)
        response = self.client.get(
            reverse('inventory:daily_material_queue'),
            {'date': today_jalali.strftime('%Y-%m-%d')},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_gregorian'], self.today)

    def test_12_default_is_today(self):
        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))
        self.assertEqual(response.context['selected_gregorian'], self.today)

    def test_13_invalid_date_falls_back_to_today_without_error(self):
        self.client.force_login(self.warehouse_user)
        response = self.client.get(
            reverse('inventory:daily_material_queue'), {'date': 'نامعتبر'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_gregorian'], self.today)

    def test_14_date_input_is_not_a_gregorian_date_widget(self):
        """input[type=date] مقدار میلادی می‌فرستاد و با parse_jalali_date ناسازگار بود."""
        source = TEMPLATE.read_text(encoding='utf-8')
        date_input_block = [
            line for line in source.splitlines()
            if 'name="date"' in line or 'id="dateFilter"' in line
        ]
        self.assertTrue(date_input_block, 'ورودی تاریخ پیدا نشد')
        for line in date_input_block:
            self.assertNotIn('type="date"', line)

    def test_15_view_uses_shared_jalali_parser(self):
        source = Path('inventory/views.py').read_text(encoding='utf-8')
        self.assertIn('parse_jalali_date', source)
        # تفسیر دستی میلادی/شمسی با strptime نباید باقی بماند
        self.assertNotIn("jdatetime.datetime.strptime(date_str", source)


class SourcesXssTests(ReviewBase):
    """C) دادهٔ متنی کاربر نباید به‌عنوان HTML وارد DOM شود."""

    def _load_sources_js(self):
        """کد تابع loadSources بدون کامنت‌ها (تا ادعاها فقط روی کد سنجیده شوند)."""
        source = TEMPLATE.read_text(encoding='utf-8')
        start = source.index('async function loadSources')
        rest = source[start + len('async function loadSources'):]
        end = rest.find('// ====')
        if end == -1:
            end = len(rest)
        body = source[start:start + len('async function loadSources') + end]
        return '\n'.join(
            line for line in body.splitlines()
            if not line.strip().startswith('//')
        )

    def _queue_with_payload(self, payload):
        # نیاز مصرف برای همین بخش رنگی هم باید وجود داشته باشد، وگرنه تسک از
        # برنامهٔ روز خارج می‌شود و صف لغو می‌شود (و منابعش پاک می‌شود).
        PaintingMaterialRequirement.objects.create(
            process=self.process, raw_material=self.raw_pack4,
            product=self.product, color_part=payload,
            consumption_per_unit=Decimal('0.500'),
        )
        task = self._task(quantity=6)
        task.color_part = payload
        task.save()
        return self._queue(raw=self.raw_pack4)

    def test_16_sources_endpoint_returns_payload_as_json_data(self):
        payload = '<img src=x onerror=alert(1)>'
        queue = self._queue_with_payload(payload)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(
            reverse('inventory:daily_queue_sources', args=[queue.pk]),
        )

        self.assertEqual(response.status_code, 200)
        # پاسخ باید داده (JSON) باشد، نه HTML رندرشده
        self.assertIn('application/json', response['Content-Type'])
        # payload فقط به‌عنوان مقدار رشته‌ای JSON برمی‌گردد؛ تزریق آن به DOM
        # وظیفهٔ کد سمت مرورگر است که در تست‌های زیر بررسی می‌شود.
        self.assertEqual(
            response.json()['rows'][0]['color_part'], payload,
        )

    def test_17_template_builds_sources_rows_with_dom_apis_not_innerhtml(self):
        load_sources = self._load_sources_js()

        self.assertNotIn('innerHTML', load_sources)
        self.assertIn('createElement', load_sources)
        self.assertIn('textContent', load_sources)

    def test_18_template_does_not_interpolate_rows_into_html_templates(self):
        load_sources = self._load_sources_js()
        self.assertNotIn('${row.', load_sources)

    def test_19_delivery_eligibility_not_derived_from_badge_text(self):
        source = TEMPLATE.read_text(encoding='utf-8')
        self.assertNotIn("querySelector('.badge')", source)
        self.assertNotIn("'در انتظار تحویل'", source)
        self.assertIn('can_deliver', source)

    def test_20_max_returnable_not_recomputed_in_javascript(self):
        source = TEMPLATE.read_text(encoding='utf-8')
        self.assertNotIn('delivered - returned', source)
        self.assertIn('max_returnable', source)


class CancelledFilterTests(ReviewBase):
    """D) فیلتر «لغو شده» باید کار کند."""

    def _cancelled_row(self):
        """همهٔ ردیف‌های آن روز لغو می‌شوند (هر تسک دو ماده دارد)."""
        self._task(quantity=6)
        rows = list(DailyMaterialQueue.objects.filter(work_date=self.today))
        self.assertTrue(rows)
        for row in rows:
            row.status = 'cancelled'
            row.save(update_fields=['status'])
        return rows

    def test_21_cancelled_rows_are_hidden_by_default(self):
        self._cancelled_row()

        report = services.daily_material_report(self.today)
        self.assertEqual(list(report['rows']), [])
        summary = report['summary']
        self.assertEqual(summary['pending_count'], 0)
        self.assertEqual(summary['total_planned'], Decimal('0.00'))

    def test_22_explicit_cancelled_filter_returns_cancelled_rows(self):
        rows = self._cancelled_row()

        report = services.daily_material_report(self.today, status='cancelled')
        report_rows = list(report['rows'])

        self.assertEqual(len(report_rows), len(rows))
        self.assertEqual(
            sorted(r.id for r in report_rows), sorted(r.id for r in rows)
        )
        for row in report_rows:
            self.assertEqual(row.status, 'cancelled')
        self.assertEqual(report['summary']['total_planned'], Decimal('3.02'))

    def test_23_cancelled_filter_on_page(self):
        rows = self._cancelled_row()

        self.client.force_login(self.warehouse_user)
        response = self.client.get(
            reverse('inventory:daily_material_queue'), {'status': 'cancelled'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['queues']), len(rows))
        self.assertEqual(response.context['summary']['total_planned'], Decimal('3.02'))

    def test_24_other_status_filters_still_exclude_cancelled(self):
        self._cancelled_row()

        report = services.daily_material_report(self.today, status='pending')
        self.assertEqual(list(report['rows']), [])

    def test_25_include_cancelled_flag_still_works(self):
        rows = self._cancelled_row()

        qs = services.daily_queue_filtered_queryset(self.today, include_cancelled=True)
        self.assertEqual(qs.count(), len(rows))


class SignalStationTests(ReviewBase):
    """E) جابه‌جایی از ایستگاه نقاشی باید صف روز قبل را هم sync کند."""

    def test_26_moving_task_off_paint_cancels_previous_queue_row(self):
        task = self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        self.assertIsNotNone(queue)
        self.assertEqual(queue.status, 'pending')
        self.assertEqual(queue.planned_quantity, Decimal('3.00'))

        # انتقال به ایستگاه دیگر: نیاز نقاشی از برنامهٔ روز خارج می‌شود
        task.station_name = 'assembly'
        task.save()

        queue.refresh_from_db()
        self.assertEqual(
            queue.status, 'cancelled',
            'صف روز قبل باید پس از خروج تسک از نقاشی sync می‌شد.',
        )

    def test_27_queue_is_revived_when_task_returns_to_paint(self):
        task = self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)

        task.station_name = 'assembly'
        task.save()
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'cancelled')

        task.station_name = 'paint'
        task.save()
        queue.refresh_from_db()

        self.assertEqual(queue.status, 'pending')
        self.assertEqual(queue.planned_quantity, Decimal('3.00'))

    def test_28_previous_state_query_reads_station_name(self):
        source = Path('inventory/signals.py').read_text(encoding='utf-8')
        pre_save = source[:source.index('@receiver(post_save')]
        self.assertIn("'station_name'", pre_save)
        # قبل و بعد نباید از یک منبع بخوانند
        self.assertIn('previous_station', pre_save)

    def test_29_moving_between_days_still_syncs_both_days(self):
        tomorrow = self.today + timezone.timedelta(days=1)
        task = self._task(quantity=6)
        self.assertIsNotNone(self._queue(raw=self.raw_pack4))

        task.scheduled_start = timezone.make_aware(
            timezone.datetime.combine(tomorrow, time(8, 0))
        )
        task.save()

        self.assertEqual(
            self._queue(raw=self.raw_pack4, date=self.today).status, 'cancelled'
        )
        self.assertEqual(
            self._queue(raw=self.raw_pack4, date=tomorrow).status, 'pending'
        )


class QuantityPrecisionTests(ReviewBase):
    """F) جمع با دقت کامل و بدون حذف نیاز کوچک."""

    def test_30_tiny_requirement_is_not_dropped_from_the_plan(self):
        # planned = 1 × 0.004 = 0.004  →  در فیلد دومقطری 0.00 می‌شود،
        # اما نباید بی‌صدا حذف شود.
        item = OrderItem.objects.create(order=self.order, product=self.product, quantity=1)
        Color.objects.create(part='بدنه', code='8', orderitem=item)
        self._task(quantity=1, item=item)

        queue = self._queue(raw=self.raw_tiny)
        self.assertIsNotNone(queue, 'ردیف صف برای نیاز کوچک ساخته نشد.')
        self.assertEqual(queue.planned_quantity, Decimal('0.00'))
        # منبع باید ثبت شده باشد تا قابل ردیابی بماند
        self.assertEqual(queue.sources.count(), 1)

    def test_31_two_tiny_requirements_accumulate_before_rounding(self):
        # دو واحد کار مستقل × ۰٫۰۰۴ = ۰٫۰۰۸ → گرد یک‌بار در انتها ⇒ ۰٫۰۱
        # (گرد کردن هر منبع جداگانه هر دو را صفر می‌کرد).
        # دو آیتم جدا لازم است: مراحل یک آیتم یک واحد کار واحد هستند و نباید
        # دوباره شمرده شوند.
        item_b = OrderItem.objects.create(order=self.order, product=self.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=item_b)
        self._task(quantity=1, start=self.day_start)
        self._task(quantity=1, start=self.day_start + timezone.timedelta(hours=2),
                   item=item_b)

        queue = self._queue(raw=self.raw_tiny)
        self.assertEqual(queue.sources.count(), 2)
        self.assertEqual(
            queue.planned_quantity, Decimal('0.01'),
            'جمع باید با دقت کامل انجام و فقط یک‌بار گرد شود.',
        )

    def test_32_normal_quantities_are_unchanged_by_the_precision_fix(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        self.assertEqual(queue.planned_quantity, Decimal('3.00'))

    def test_33_three_decimal_consumption_rounds_once(self):
        PaintingMaterialRequirement.objects.create(
            process=self.process, raw_material=self.raw, product=self.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.125'),
        )
        item = OrderItem.objects.create(order=self.order, product=self.product, quantity=3)
        Color.objects.create(part='بدنه', code='8', orderitem=item)
        self._task(quantity=3, item=item)

        queue = self._queue(raw=self.raw)
        # ۳ × ۰٫۱۲۵ = ۰٫۳۷۵  →  ۰٫۳۸
        self.assertEqual(queue.planned_quantity, Decimal('0.38'))

    def test_34_zero_consumption_is_still_ignored(self):
        PaintingMaterialRequirement.objects.create(
            process=self.process, raw_material=self.raw, product=self.product,
            color_part='بدنه', consumption_per_unit=Decimal('0'),
        )
        item = OrderItem.objects.create(order=self.order, product=self.product, quantity=5)
        Color.objects.create(part='بدنه', code='8', orderitem=item)
        self._task(quantity=5, item=item)

        self.assertIsNone(
            self._queue(raw=self.raw),
            'نیاز صفر نباید ردیف صف بسازد.',
        )


class ActionStateTests(ReviewBase):
    """G) وضعیت اقدام‌ها از سرویس می‌آید."""

    def test_35_pending_row_can_deliver_and_cannot_return(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)

        state = services.daily_queue_action_state(queue)
        self.assertTrue(state['can_deliver'])
        self.assertFalse(state['can_return'])
        self.assertEqual(state['max_returnable'], '0.00')
        self.assertEqual(state['status'], 'pending')

    def test_36_delivered_row_can_return_full_physical(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()

        state = services.daily_queue_action_state(queue)
        self.assertFalse(state['can_deliver'])
        self.assertTrue(state['can_return'])
        self.assertEqual(state['max_returnable'], '4.00')

    def test_37_fully_returned_row_cannot_return_again(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='4.00',
        )
        queue.refresh_from_db()

        state = services.daily_queue_action_state(queue)
        self.assertFalse(state['can_return'])
        self.assertEqual(state['max_returnable'], '0.00')

    def test_38_cancelled_row_can_do_nothing(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        queue.status = 'cancelled'
        queue.save(update_fields=['status'])

        state = services.daily_queue_action_state(queue)
        self.assertFalse(state['can_deliver'])
        self.assertFalse(state['can_return'])

    def test_39_state_matches_actual_service_behaviour(self):
        """آنچه سرویس می‌گوید مجاز است، واقعاً مجاز باشد."""
        from inventory.services import HandoverError

        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)

        state = services.daily_queue_action_state(queue)
        if not state['can_deliver']:
            with self.assertRaises(HandoverError):
                services.execute_daily_delivery(
                    queue_id=queue.pk, delivered_by=self.superuser,
                )

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()
        state = services.daily_queue_action_state(queue)

        if state['can_return']:
            services.execute_daily_return(
                queue_id=queue.pk, returned_by=self.superuser,
                returned_quantity=state['max_returnable'],
            )
        else:
            with self.assertRaises(HandoverError):
                services.execute_daily_return(
                    queue_id=queue.pk, returned_by=self.superuser,
                    returned_quantity='1.00',
                )

    def test_40_endpoints_expose_action_state(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)

        self.client.force_login(self.warehouse_user)
        preview = self.client.get(
            reverse('inventory:daily_queue_preview_delivery', args=[queue.pk]),
        )
        self.assertTrue(preview.json()['can_deliver'])
        self.assertIn('status', preview.json())

        sources = self.client.get(
            reverse('inventory:daily_queue_sources', args=[queue.pk]),
        )
        payload = sources.json()
        self.assertIn('can_deliver', payload)
        self.assertIn('can_return', payload)
        self.assertIn('max_returnable', payload)
        self.assertEqual(payload['max_returnable'], '0.00')


class RemovedDryRunTests(ReviewBase):
    """H) پارامتر بی‌اثر حذف شده باشد."""

    def test_41_sync_helpers_have_no_dry_run_parameter(self):
        import inspect

        for func in (services.sync_queue_for_tasks, services.sync_queue_for_date):
            params = inspect.signature(func).parameters
            self.assertNotIn(
                'dry_run', params,
                f'{func.__name__} نباید پارامتر بی‌اثر dry_run داشته باشد.',
            )

    def test_42_no_dry_run_callers_remain(self):
        for path in ('inventory/services.py', 'inventory/signals.py', 'product/utils.py'):
            source = Path(path).read_text(encoding='utf-8')
            self.assertNotIn('dry_run', source, f'{path} هنوز dry_run دارد.')


class SummaryQueryTests(ReviewBase):
    """I) جمع روزانه با یک aggregate."""

    def test_43_summary_runs_exactly_one_query(self):
        # سه واحد کار مستقل (سه آیتم جدا): هر کدام برای دو ماده ردیف می‌سازد،
        # اما چون کارگر و تاریخ یکسان است در ۲ ردیف صف ادغام می‌شوند:
        # ۹٫۰۰ برای پک‌دار + ۰٫۰۷ برای مادهٔ کوچک.
        for _ in range(3):
            item = OrderItem.objects.create(
                order=self.order, product=self.product, quantity=2,
            )
            Color.objects.create(part='بدنه', code='8', orderitem=item)
            self._task(quantity=6, item=item)

        qs = services.daily_queue_filtered_queryset(self.today)
        with CaptureQueriesContext(connection) as captured:
            summary = services.daily_queue_summary(qs)

        self.assertEqual(
            len(captured), 1,
            'جمع روزانه باید با یک کوئری aggregate انجام شود.',
        )
        # هر تسک برای دو ماده ردیف می‌سازد، اما چون کارگر و تاریخ یکسان است
        # در ۲ ردیف صف ادغام می‌شوند: ۹٫۰۰ برای پک‌دار + ۰٫۰۷ برای مادهٔ کوچک
        self.assertEqual(summary['total_planned'], Decimal('9.07'))
        self.assertEqual(summary['pending_count'], 2)

    def test_44_summary_conflict_count_uses_single_query(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)
        queue.has_plan_conflict = True
        queue.save(update_fields=['has_plan_conflict'])

        qs = services.daily_queue_filtered_queryset(self.today)
        with CaptureQueriesContext(connection) as captured:
            summary = services.daily_queue_summary(qs)

        self.assertEqual(len(captured), 1)
        self.assertEqual(summary['conflict_count'], 1)

    def test_45_summary_has_no_unused_closed_count(self):
        self._task(quantity=6)
        qs = services.daily_queue_filtered_queryset(self.today)
        self.assertNotIn('closed_count', services.daily_queue_summary(qs))


class PackCeilingTests(ReviewBase):
    """J) یک قانون مشخص و اعشاری برای سقف بسته‌بندی."""

    def test_46_tiny_pack_does_not_over_deliver(self):
        # ۰٫۰۷ نیاز با بستهٔ ۰٫۰۱ ⇒ ۷ بسته (نه ۸ بسته)
        self.assertEqual(services._packs_counted('0.07', '0.01'), 7)
        self.assertEqual(services._physical_for('0.07', '0.01'), (7, Decimal('0.07')))

    def test_47_float_ceiling_would_have_been_wrong(self):
        """تست رگرسیون: روش اعشاری قدیمی ۸ بسته می‌داد."""
        import math
        self.assertEqual(
            int(math.ceil(float(Decimal('0.07')) / float(Decimal('0.01')))), 8
        )
        self.assertEqual(services._packs_counted('0.07', '0.01'), 7)

    def test_48_ceiling_matches_legacy_decimal_rule(self):
        from decimal import ROUND_CEILING

        for need, pack in (
            ('3', '4'), ('0.07', '0.01'), ('1', '3'), ('2.5', '1.5'),
            ('10', '0.3'), ('0.01', '0.01'), ('99.99', '0.05'),
        ):
            n, p = Decimal(need), Decimal(pack)
            legacy = int((n / p).to_integral_value(rounding=ROUND_CEILING))
            self.assertEqual(
                services._packs_counted(n, p), legacy,
                f'اختلاف برای need={need} pack={pack}',
            )

    def test_49_delivery_and_preview_agree_on_pack_count(self):
        self._task(quantity=6)
        queue = self._queue(raw=self.raw_pack4)

        preview = services.preview_daily_delivery(queue.pk)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        queue.refresh_from_db()

        self.assertEqual(
            Decimal(str(preview['physical'])), queue.delivered_quantity
        )
        self.assertEqual(preview['packs'], 1)

    def test_50_no_pack_size_delivers_exact_need(self):
        self.assertEqual(services._physical_for('3.5', '0'), (0, Decimal('3.50')))

    def test_51_zero_need_has_no_packs(self):
        self.assertEqual(services._packs_counted('0', '4'), 0)
        self.assertEqual(services._physical_for('0', '4'), (0, Decimal('0.00')))
