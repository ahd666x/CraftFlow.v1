"""
Phase 4 — صف مواد روزانه انبار (Warehouse Daily Material Queue UI).

این تست‌ها فقط رابط کاربری و تعامل آن با سرویس‌های موجود Phase 3 را می‌پوشانند:
    صفحه بالا می‌آید، فیلترها کار می‌کنند، تحویل/برگشت از همان سرویس مرکزی
    استفاده می‌کنند و هیچ دست‌کاری مستقیم موجودی در UI انجام نمی‌شود.
"""
import json
from datetime import datetime, time, timedelta
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


class DailyQueueUIBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('q4admin', password='pw')

        # کاربر انبار (عضو گروه «انبار») و کاربر عادی بدون دسترسی
        cls.warehouse_user = User.objects.create_user('q4warehouse', password='pw')
        warehouse_group, _ = Group.objects.get_or_create(name='انبار')
        cls.warehouse_user.groups.add(warehouse_group)

        cls.plain_user = User.objects.create_user('q4plain', password='pw')

        cls.worker_a = User.objects.create_user('q4worker_a', password='pw', first_name='حسین')
        cls.worker_b = User.objects.create_user('q4worker_b', password='pw', first_name='علی')

        cls.raw_category = RawMaterialCategory.objects.create(name='مواد نقاشی')
        cls.raw = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ سفید', code='R-WHITE',
            unit='kg', pack_size=Decimal('0'),
        )
        cls.raw_kg4 = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ کیلر', code='R-KILLER',
            unit='kg', pack_size=Decimal('4'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته تست')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول تست', base_price=1000,
        )

        cls.process = PaintingProcess.objects.create(
            name='روند نقاشی', code='P4', color_codes=['8'], is_active=True,
        )
        cls.stage_1 = PaintingStage.objects.create(
            process=cls.process, order=1, name='زیرکار',
            duration_minutes=30, drying_time_minutes=0,
        )

        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.superuser, customer=cls.customer, number='Q4-1',
        )

        def _make_item(label):
            item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
            Color.objects.create(part='بدنه', code='8', orderitem=item)
            return item

        cls.order_item = _make_item('main')
        cls.order_item_2 = _make_item('second')

        PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.raw, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('0.500'),
        )

        # موجودی اولیه کافی برای تحویل
        StockMovement.objects.create(
            raw_material=cls.raw, movement_type='purchase',
            quantity=Decimal('100'), created_by=cls.superuser,
        )
        StockMovement.objects.create(
            raw_material=cls.raw_kg4, movement_type='purchase',
            quantity=Decimal('50'), created_by=cls.superuser,
        )

        # «امروز» برای تست‌ها (زمان آگاه می‌سازیم تا به تاریخ کاری درست بخورد)
        today = timezone.localdate()
        cls.today_date = today
        cls.today_jalali = jdatetime.date.fromgregorian(date=today)
        cls.day_start = timezone.make_aware(datetime.combine(today, time(8, 0)))
        cls.tomorrow_start = cls.day_start + timedelta(days=1)

    def _make_task(self, *, worker=None, start=None, quantity=2, order_item=None, stage=None):
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
            scheduled_start=start or self.day_start,
            scheduled_end=None,
        )

    def _queue(self, work_date=None, worker=None, raw=None):
        return DailyMaterialQueue.objects.filter(
            work_date=work_date or self.today_date,
            worker=worker or self.worker_a,
            raw_material=raw or self.raw,
        ).first()

    def _ajax(self):
        return {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}


class DailyQueuePageTests(DailyQueueUIBase):
    def test_01_page_loads_for_warehouse_user(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/daily_material_queue.html')
        # صفحه نباید خطای سرور بدهد
        self.assertNotIn(b'Internal Server Error', response.content)

    def test_02_todays_queue_is_shown(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))

        queues = response.context['queues']
        self.assertEqual(len(queues), 1)
        self.assertEqual(queues[0].worker, self.worker_a)
        self.assertEqual(queues[0].raw_material, self.raw)
        self.assertEqual(queues[0].status, 'pending')

    def test_03_default_date_is_today(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))

        self.assertEqual(response.context['selected_gregorian'], self.today_date)
        self.assertEqual(response.context['date_str'], self.today_jalali.strftime('%Y-%m-%d'))

    def test_04_date_filter_works(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        # تسکِ فردا باید صف جداگانه بسازد
        self._make_task(worker=self.worker_a, start=self.tomorrow_start, quantity=2)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))
        self.assertEqual(len(response.context['queues']), 1)
        self.assertEqual(response.context['queues'][0].work_date, self.today_date)

        tomorrow_jalali = jdatetime.date.fromgregorian(date=self.tomorrow_start.date())
        response = self.client.get(reverse('inventory:daily_material_queue'), {
            'date': tomorrow_jalali.strftime('%Y-%m-%d'),
        })
        self.assertEqual(len(response.context['queues']), 1)
        self.assertEqual(response.context['queues'][0].work_date, self.tomorrow_start.date())

    def test_05_planned_quantity_is_displayed_and_summary_aggregated(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=3, order_item=self.order_item_2)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))

        queue = self._queue()
        # ۲ واحد × ۰٫۵ + ۳ واحد × ۰٫۵ = ۲٫۵
        self.assertEqual(queue.planned_quantity, Decimal('2.50'))
        self.assertContains(response, '2.50')

        summary = response.context['summary']
        self.assertEqual(summary['total_planned'], Decimal('2.50'))
        self.assertEqual(response.context['total_count'], 1)

    def test_06_separate_quantity_from_actual_consumption(self):
        """مقدار برنامه‌ای نباید با مصرف واقعی یکی شود."""
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))

        self.assertEqual(response.context['summary']['total_actual'], Decimal('0.00'))
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))
        self.assertEqual(queue.actual_consumption, Decimal('0.00'))

    def test_07_worker_filter_works(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        self._make_task(worker=self.worker_b, start=self.day_start, quantity=2, order_item=self.order_item_2)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))
        self.assertEqual(len(response.context['queues']), 2)

        response = self.client.get(reverse('inventory:daily_material_queue'), {
            'worker': str(self.worker_b.pk),
        })
        queues = response.context['queues']
        self.assertEqual(len(queues), 1)
        self.assertEqual(queues[0].worker, self.worker_b)

    def test_08_status_filter_works(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()
        self.assertIsNotNone(queue)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'), {
            'status': 'pending',
        })
        self.assertEqual(len(response.context['queues']), 1)

        response = self.client.get(reverse('inventory:daily_material_queue'), {
            'status': 'delivered',
        })
        self.assertEqual(len(response.context['queues']), 0)

    def test_09_material_filter_works(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'), {
            'material': str(self.raw.pk),
        })
        self.assertEqual(len(response.context['queues']), 1)

        response = self.client.get(reverse('inventory:daily_material_queue'), {
            'material': str(self.raw_kg4.pk),
        })
        self.assertEqual(len(response.context['queues']), 0)

    def test_10_conflict_is_visible(self):
        task = self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        # تغییر برنامه پس از تحویل → تعارض
        task.quantity = 6
        task.save()

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))

        queue.refresh_from_db()
        self.assertTrue(queue.has_plan_conflict)
        self.assertTrue(queue.conflict_note)
        self.assertContains(response, 'مغایرت')

    def test_11_unassigned_task_does_not_appear_as_queue_row(self):
        """تسک بدون کارگر تخصیص‌یافته نباید ردیف عادی صف داشته باشد."""
        self._make_task(worker=None, start=self.day_start, quantity=2)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))

        queues = response.context['queues']
        active = [q for q in queues if q.status != 'cancelled']
        self.assertEqual(len(active), 0)
        self.assertEqual(
            DailyMaterialQueue.objects.filter(
                work_date=self.today_date, status='pending'
            ).count(),
            0,
        )


class DailyQueueActionTests(DailyQueueUIBase):
    def test_20_delivery_uses_existing_service(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))

        stock_before = self.raw.current_stock

        self.client.force_login(self.warehouse_user)
        response = self.client.post(
            reverse('inventory:daily_queue_delivery', args=[queue.pk]),
            {'note': 'تحویل روزانه'},
            **self._ajax(),
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'])

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(queue.delivered_quantity, Decimal('1.00'))
        self.assertEqual(queue.actual_consumption, Decimal('1.00'))

        # سرویس مرکزی حرکت مصرف ساخته است؛ UI خودش موجودی را دستکاری نکرده
        movement = StockMovement.objects.filter(
            raw_material=self.raw, movement_type='consumption'
        ).order_by('-id').first()
        self.assertIsNotNone(movement)
        self.assertEqual(movement.quantity, Decimal('1.00'))
        self.assertEqual(self.raw.current_stock, stock_before - Decimal('1.00'))

    def test_21_delivery_applies_pack_size_rounding(self):
        """نیاز ۱ کیلو با بستهٔ ۴ کیلویی → خروج فیزیکی ۴ کیلو."""
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        # نیاز این ماده را به مادهٔ بسته‌دار تغییر می‌دهیم
        queue = self._queue()
        queue.raw_material = self.raw_kg4
        queue.planned_quantity = Decimal('1.00')
        queue.save()

        self.client.force_login(self.warehouse_user)
        response = self.client.post(
            reverse('inventory:daily_queue_delivery', args=[queue.pk]),
            {}, **self._ajax(),
        )

        self.assertEqual(response.status_code, 200)
        queue.refresh_from_db()
        # delivered_quantity مقدار فیزیکی تحویل‌شده (پک گرد‌شده) است، نه مقدار نیاز.
        self.assertEqual(queue.delivered_quantity, Decimal('4.00'))
        self.assertEqual(queue.actual_consumption, Decimal('4.00'))
        movement = StockMovement.objects.filter(
            raw_material=self.raw_kg4, movement_type='consumption'
        ).order_by('-id').first()
        self.assertEqual(movement.quantity, Decimal('4.00'))

    def test_22_delivery_rejected_for_insufficient_stock(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()
        queue.raw_material = self.raw_kg4
        queue.planned_quantity = Decimal('100.00')
        queue.save()

        self.client.force_login(self.warehouse_user)
        response = self.client.post(
            reverse('inventory:daily_queue_delivery', args=[queue.pk]),
            {}, **self._ajax(),
        )

        self.assertEqual(response.status_code, 400)
        payload = json.loads(response.content)
        self.assertFalse(payload['success'])

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')
        self.assertEqual(queue.delivered_quantity, Decimal('0.00'))

    def test_23_return_uses_existing_service(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        stock_after_delivery = self.raw.current_stock

        self.client.force_login(self.warehouse_user)
        response = self.client.post(
            reverse('inventory:daily_queue_return', args=[queue.pk]),
            {'returned_quantity': '0.40', 'note': 'برگشت پایان روز'},
            **self._ajax(),
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'])

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.40'))
        self.assertEqual(queue.actual_consumption, Decimal('0.60'))

        # برگشتی = موجودی عمومی انبار (بدون امانت کارگر)
        self.assertEqual(self.raw.current_stock, stock_after_delivery + Decimal('0.40'))
        self.assertFalse(
            self.raw.custodies.exists(),
            'برگشتی نباید امانت کارگر بسازد.',
        )

    def test_24_return_cannot_exceed_delivered(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        self.client.force_login(self.warehouse_user)
        response = self.client.post(
            reverse('inventory:daily_queue_return', args=[queue.pk]),
            {'returned_quantity': '5.00'},
            **self._ajax(),
        )

        self.assertEqual(response.status_code, 400)
        payload = json.loads(response.content)
        self.assertFalse(payload['success'])

        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))

    def test_25_delivery_requires_ajax_header(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()

        self.client.force_login(self.warehouse_user)
        response = self.client.post(
            reverse('inventory:daily_queue_delivery', args=[queue.pk]), {},
        )
        self.assertEqual(response.status_code, 403)

    def test_26_sources_endpoint_shows_traceability(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()
        self.assertEqual(queue.sources.count(), 1)

        self.client.force_login(self.warehouse_user)
        response = self.client.get(
            reverse('inventory:daily_queue_sources', args=[queue.pk]), **self._ajax(),
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'])
        self.assertEqual(len(payload['rows']), 1)
        self.assertEqual(payload['rows'][0]['stage_name'], self.stage_1.name)
        self.assertEqual(payload['rows'][0]['quantity'], '1.00')

    def test_27_preview_endpoint_reports_pack_rounding(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()
        queue.raw_material = self.raw_kg4
        queue.planned_quantity = Decimal('1.00')
        queue.save()

        self.client.force_login(self.warehouse_user)
        response = self.client.get(
            reverse('inventory:daily_queue_preview_delivery', args=[queue.pk]),
            **self._ajax(),
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['packs'], 1)
        self.assertEqual(payload['physical'], '4.00')
        self.assertTrue(payload['enough'])


class DailyQueuePermissionTests(DailyQueueUIBase):
    def test_30_superuser_can_view(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('inventory:daily_material_queue'))
        self.assertEqual(response.status_code, 200)

    def test_31_warehouse_group_can_view(self):
        self.client.force_login(self.warehouse_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))
        self.assertEqual(response.status_code, 200)

    def test_32_unprivileged_user_is_redirected(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)

        self.client.force_login(self.plain_user)
        response = self.client.get(reverse('inventory:daily_material_queue'))

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.url, reverse('inventory:daily_material_queue'))

    def test_33_unprivileged_user_cannot_deliver(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()

        self.client.force_login(self.plain_user)
        response = self.client.post(
            reverse('inventory:daily_queue_delivery', args=[queue.pk]),
            {}, **self._ajax(),
        )

        self.assertEqual(response.status_code, 403)
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')
        self.assertEqual(queue.delivered_quantity, Decimal('0.00'))

    def test_34_unprivileged_user_cannot_return(self):
        self._make_task(worker=self.worker_a, start=self.day_start, quantity=2)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        self.client.force_login(self.plain_user)
        response = self.client.post(
            reverse('inventory:daily_queue_return', args=[queue.pk]),
            {'returned_quantity': '0.20'},
            **self._ajax(),
        )

        self.assertEqual(response.status_code, 403)
        queue.refresh_from_db()
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))

    def test_35_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse('inventory:daily_material_queue'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.url)