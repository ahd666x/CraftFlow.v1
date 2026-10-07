"""
Phase 5 — اجرای تحویل مواد روزانه (Warehouse Delivery Execution).

تمرکز این تست‌ها روی رفتار «اجرای تحویل» است، نه محاسبهٔ صف:

    صف → تأیید انبار → execute_daily_delivery() → StockMovement(consumption)
       → delivered_quantity به‌روز → status به‌روز

موارد تحت پوشش: تحویل موفق، گردکردن بسته، نبود pack_size، کمبود موجودی،
تحویل تکراری، تراکنش/rollback اتمیک، عدم تغییر planned، ایجاد حرکت انبار،
و رد کاربر غیرمجاز.
"""
import json
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest import mock

import jdatetime

from inventory import services
from inventory.models import (
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


class DeliveryExecutionBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('p5admin', password='pw')
        cls.warehouse_user = User.objects.create_user('p5warehouse', password='pw')
        group, _ = Group.objects.get_or_create(name='انبار')
        cls.warehouse_user.groups.add(group)
        cls.plain_user = User.objects.create_user('p5plain', password='pw')

        cls.worker = User.objects.create_user('p5worker', password='pw', first_name='حسین')

        cls.raw_category = RawMaterialCategory.objects.create(name='مواد نقاشی')
        # بدون بسته‌بندی
        cls.raw = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ سفید', code='R-WHITE',
            unit='kg', pack_size=Decimal('0'),
        )
        # با بسته‌بندی ۴ کیلویی
        cls.raw_pack4 = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ کیلر', code='R-KILLER',
            unit='kg', pack_size=Decimal('4'),
        )
        # بسته‌بندی نامعتبر (صفر) باید مثل «بدون بسته» رفتار کند
        cls.raw_pack0 = RawMaterial.objects.create(
            category=cls.raw_category, name='رنگ خام', code='R-RAW',
            unit='lit', pack_size=Decimal('0'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته تست')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول تست', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند نقاشی', code='P5', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='زیرکار',
            duration_minutes=30, drying_time_minutes=0,
        )
        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.superuser, customer=cls.customer, number='P5-1',
        )
        cls.order_item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=2,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)

        for raw in (cls.raw, cls.raw_pack4, cls.raw_pack0):
            PaintingMaterialRequirement.objects.create(
                process=cls.process, stage=cls.stage, raw_material=raw, product=cls.product,
                color_part='بدنه', consumption_per_unit=Decimal('0.500'),
            )

        for raw, qty in ((cls.raw, '100'), (cls.raw_pack4, '50'), (cls.raw_pack0, '40')):
            StockMovement.objects.create(
                raw_material=raw, movement_type='purchase',
                quantity=Decimal(qty), created_by=cls.superuser,
            )

        cls.today_date = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.today_date, time(8, 0))
        )

    def _make_task(self, *, quantity=2, stage=None):
        return ProductionTask.objects.create(
            order=self.order,
            order_item=self.order_item,
            station_name='paint',
            step_order=1,
            quantity=quantity,
            status='pending',
            painting_stage=stage or self.stage,
            color_part='بدنه',
            assigned_worker=self.worker,
            scheduled_start=self.day_start,
            scheduled_end=None,
        )

    def _queue(self, raw=None):
        return DailyMaterialQueue.objects.filter(
            work_date=self.today_date,
            worker=self.worker,
            raw_material=raw or self.raw,
        ).first()

    def _build_queue(self, raw, planned):
        """یک صف با مقدار برنامه‌ای مشخص می‌سازد (بدون نیاز به فرمول نقاشی)."""
        return DailyMaterialQueue.objects.create(
            work_date=self.today_date,
            worker=self.worker,
            raw_material=raw,
            planned_quantity=Decimal(planned),
            status='pending',
        )

    def _ajax(self):
        return {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}

    def _post_delivery(self, queue, user=None):
        client = self.client
        client.force_login(user or self.warehouse_user)
        return client.post(
            reverse('inventory:daily_queue_delivery', args=[queue.pk]),
            {}, **self._ajax(),
        )


class SuccessfulDeliveryTests(DeliveryExecutionBase):
    def test_01_successful_delivery_creates_consumption_movement(self):
        self._make_task(quantity=2)
        queue = self._queue()
        # ۲ واحد × ۰٫۵ = ۱ کیلو نیاز
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))

        stock_before = self.raw.current_stock
        movements_before = StockMovement.objects.filter(
            raw_material=self.raw, movement_type='consumption'
        ).count()

        result = services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.superuser,
        )

        queue.refresh_from_db()
        self.assertEqual(result.pk, queue.pk)
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(queue.delivered_quantity, Decimal('1.00'))
        self.assertEqual(queue.actual_consumption, Decimal('1.00'))

        # حرکت انبار از همان نوع موجود ساخته شده است
        movements = StockMovement.objects.filter(
            raw_material=self.raw, movement_type='consumption'
        )
        self.assertEqual(movements.count(), movements_before + 1)
        movement = movements.order_by('-id').first()
        self.assertEqual(movement.quantity, Decimal('1.00'))
        self.assertEqual(movement.created_by, self.superuser)

        # موجودی از طریق دفتر حرکات کم شده، نه با دستکاری مستقیم
        self.assertEqual(self.raw.current_stock, stock_before - Decimal('1.00'))

    def test_02_planned_quantity_unchanged_by_delivery(self):
        self._make_task(quantity=2)
        queue = self._queue()
        planned_before = queue.planned_quantity
        sources_before = list(
            queue.sources.values_list('production_task_id', 'painting_stage_id', 'quantity')
        )

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        queue.refresh_from_db()
        self.assertEqual(queue.planned_quantity, planned_before)
        self.assertEqual(
            list(queue.sources.values_list('production_task_id', 'painting_stage_id', 'quantity')),
            sources_before,
            'منابع برنامه‌ریزی نباید با تحویل تغییر کنند.',
        )

    def test_03_queue_history_fields_are_separate(self):
        self._make_task(quantity=2)
        queue = self._queue()
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        queue.refresh_from_db()
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))
        self.assertEqual(queue.delivered_quantity, Decimal('1.00'))
        self.assertEqual(queue.returned_quantity, Decimal('0.00'))
        self.assertEqual(queue.actual_consumption, Decimal('1.00'))

        # برگشتی، مصرف واقعی را کم می‌کند ولی planned را نه
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='0.40',
        )
        queue.refresh_from_db()
        self.assertEqual(queue.planned_quantity, Decimal('1.00'))
        self.assertEqual(queue.returned_quantity, Decimal('0.40'))
        self.assertEqual(queue.actual_consumption, Decimal('0.60'))


class PackageRoundingTests(DeliveryExecutionBase):
    def test_04_package_rounding_up(self):
        """نیاز ۳ کیلو با بستهٔ ۴ کیلویی → خروج فیزیکی ۴ کیلو."""
        queue = self._build_queue(self.raw_pack4, '3')

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        queue.refresh_from_db()
        movement = StockMovement.objects.filter(
            raw_material=self.raw_pack4, movement_type='consumption'
        ).order_by('-id').first()
        self.assertEqual(movement.quantity, Decimal('4.00'))
        # delivered_quantity مقدار فیزیکی تحویل‌شده (پک گرد‌شده) است، نه مقدار نیاز.
        self.assertEqual(queue.delivered_quantity, Decimal('4.00'))

    def test_05_package_exact_multiple_no_over_delivery(self):
        """نیاز ۴ کیلو با بستهٔ ۴ کیلویی → دقیقاً ۴ کیلو."""
        queue = self._build_queue(self.raw_pack4, '4')

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        movement = StockMovement.objects.filter(
            raw_material=self.raw_pack4, movement_type='consumption'
        ).order_by('-id').first()
        self.assertEqual(movement.quantity, Decimal('4.00'))

    def test_06_two_packages_for_five_kg(self):
        """نیاز ۵ کیلو با بستهٔ ۴ کیلویی → ۲ بسته = ۸ کیلو."""
        queue = self._build_queue(self.raw_pack4, '5')

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        movement = StockMovement.objects.filter(
            raw_material=self.raw_pack4, movement_type='consumption'
        ).order_by('-id').first()
        self.assertEqual(movement.quantity, Decimal('8.00'))

    def test_07_no_pack_size_delivers_exact_planned(self):
        """بدون pack_size → تحویل دقیقاً برابر مقدار برنامه."""
        queue = self._build_queue(self.raw, '2.75')

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        movement = StockMovement.objects.filter(
            raw_material=self.raw, movement_type='consumption'
        ).order_by('-id').first()
        self.assertEqual(movement.quantity, Decimal('2.75'))

    def test_08_zero_pack_size_behaves_like_no_package(self):
        queue = self._build_queue(self.raw_pack0, '3.50')

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        movement = StockMovement.objects.filter(
            raw_material=self.raw_pack0, movement_type='consumption'
        ).order_by('-id').first()
        self.assertEqual(movement.quantity, Decimal('3.50'))

    def test_09_preview_matches_executed_delivery(self):
        """پیش‌نمایش UI باید دقیقاً همان چیزی باشد که اجرا می‌شود."""
        queue = self._build_queue(self.raw_pack4, '3')

        preview = services.preview_daily_delivery(queue.pk)

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        movement = StockMovement.objects.filter(
            raw_material=self.raw_pack4, movement_type='consumption'
        ).order_by('-id').first()
        self.assertEqual(str(preview['physical']), str(movement.quantity))
        self.assertEqual(preview['packs'], 1)


class StockSafetyTests(DeliveryExecutionBase):
    def test_10_insufficient_stock_creates_no_movement(self):
        queue = self._build_queue(self.raw, '500')

        stock_before = self.raw.current_stock
        movements_before = StockMovement.objects.count()

        with self.assertRaises(HandoverError) as ctx:
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser,
            )

        self.assertIn('موجودی انبار کافی نیست', str(ctx.exception))

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')
        self.assertEqual(queue.delivered_quantity, Decimal('0.00'))
        self.assertEqual(StockMovement.objects.count(), movements_before)
        self.assertEqual(self.raw.current_stock, stock_before)

    def test_11_insufficient_stock_after_pack_rounding(self):
        """موجودی برای نیاز کافی است اما برای بستهٔ کامل کافی نیست."""
        raw = self.raw_pack4
        StockMovement.objects.create(
            raw_material=raw, movement_type='consumption',
            quantity=Decimal('48'), created_by=self.superuser, note='کاهش موجودی',
        )
        queue = self._build_queue(raw, '4')
        # موجودی ۲ کیلو، ولی یک بستهٔ کامل ۴ کیلو لازم است
        self.assertEqual(raw.current_stock, Decimal('2.00'))

        with self.assertRaises(HandoverError):
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser,
            )

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')
        self.assertEqual(queue.delivered_quantity, Decimal('0.00'))

    def test_12_zero_planned_is_rejected(self):
        queue = self._build_queue(self.raw, '0')

        with self.assertRaises(HandoverError):
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser,
            )

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw, movement_type='consumption'
            ).count(),
            0,
        )

    def test_13_cancelled_queue_cannot_be_delivered(self):
        self._make_task(quantity=2)
        queue = self._queue()
        queue.status = 'cancelled'
        queue.save(update_fields=['status'])

        with self.assertRaises(HandoverError):
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser,
            )

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'cancelled')
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw, movement_type='consumption'
            ).count(),
            0,
        )


class IdempotencyTests(DeliveryExecutionBase):
    def test_14_duplicate_delivery_rejected(self):
        queue = self._build_queue(self.raw_pack4, '3')

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        stock_after_first = self.raw_pack4.current_stock
        movements_after_first = StockMovement.objects.count()

        with self.assertRaises(HandoverError) as ctx:
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser,
            )

        self.assertIn('قبلاً تحویل داده شده', str(ctx.exception))
        self.assertEqual(self.raw_pack4.current_stock, stock_after_first)
        self.assertEqual(StockMovement.objects.count(), movements_after_first)

    def test_15_repeated_post_consumes_stock_only_once(self):
        """تکرار درخواست POST نباید موجودی را دوباره کم کند."""
        queue = self._build_queue(self.raw_pack4, '3')
        stock_before = self.raw_pack4.current_stock

        first = self._post_delivery(queue)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.raw_pack4.current_stock, stock_before - Decimal('4.00'))

        second = self._post_delivery(queue)
        self.assertEqual(second.status_code, 400)
        payload = json.loads(second.content)
        self.assertFalse(payload['success'])
        self.assertIn('قبلاً تحویل داده شده', payload['error'])

        # دقیقاً یک بار مصرف
        self.assertEqual(self.raw_pack4.current_stock, stock_before - Decimal('4.00'))
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw_pack4, movement_type='consumption'
            ).count(),
            1,
        )

    def test_16_delivery_after_return_is_rejected(self):
        """ردیفی که تراکنش دارد (حتی با وضعیت «برگشت») دوباره تحویل نمی‌شود."""
        queue = self._build_queue(self.raw, '4')
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='4',
        )

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'returned')
        stock_before = self.raw.current_stock

        with self.assertRaises(HandoverError):
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser,
            )

        self.assertEqual(self.raw.current_stock, stock_before)
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw, movement_type='consumption'
            ).count(),
            1,
        )

    def test_17_partial_return_still_blocks_redelivery(self):
        queue = self._build_queue(self.raw, '4')
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)
        services.execute_daily_return(
            queue_id=queue.pk, returned_by=self.superuser, returned_quantity='1',
        )

        queue.refresh_from_db()
        self.assertEqual(queue.status, 'delivered')
        stock_before = self.raw.current_stock

        with self.assertRaises(HandoverError):
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser,
            )

        self.assertEqual(self.raw.current_stock, stock_before)

    def test_18_missing_queue_raises_persian_error(self):
        with self.assertRaises(HandoverError) as ctx:
            services.execute_daily_delivery(
                queue_id=999999, delivered_by=self.superuser,
            )
        self.assertIn('یافت نشد', str(ctx.exception))

    def test_19_delivery_is_rounded_up_to_whole_packs(self):
        """
        در Engine B تحویل «به ازای هر ردیف صف» است و مقدارش از قانون بسته‌بندی
        می‌آید: نیاز ۲ کیلویی با بستهٔ ۴ کیلویی یعنی یک بستهٔ کامل (۴ کیلو).

        پارامتر ``items`` قدیمی حذف شده چون یک صف، یک نیاز دارد؛ دادن فهرست
        نیاز به تحویل یعنی پذیرفتن کسری بسته و ناسازگاری موجودی فیزیکی.
        """
        queue = self._build_queue(self.raw_pack4, '2')

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.superuser,
        )

        queue.refresh_from_db()
        self.assertEqual(queue.delivered_quantity, Decimal('4.00'))
        movement = StockMovement.objects.filter(
            raw_material=self.raw_pack4, movement_type='consumption'
        ).order_by('-id').first()
        # ۲ کیلو نیاز → یک بستهٔ ۴ کیلویی
        self.assertEqual(movement.quantity, Decimal('4.00'))

    def test_19b_delivery_service_rejects_a_legacy_items_argument(self):
        """API قدیمی نباید بی‌صدا کار کند؛ خطای روشن بدهد."""
        queue = self._build_queue(self.raw_pack4, '8')

        with self.assertRaises(TypeError):
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.superuser,
                items=[(1, Decimal('2'))],
            )


class AtomicityTests(DeliveryExecutionBase):
    def test_20_rollback_when_queue_save_fails(self):
        """اگر به‌روزرسانی صف شکست بخورد، حرکت انبار هم نباید بماند."""
        queue = self._build_queue(self.raw, '2')
        stock_before = self.raw.current_stock
        movements_before = StockMovement.objects.count()

        with mock.patch.object(
            DailyMaterialQueue, 'save',
            side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                services.execute_daily_delivery(
                    queue_id=queue.pk, delivered_by=self.superuser,
                )

        # تراکنش باید کامل برگشته باشد
        self.assertEqual(StockMovement.objects.count(), movements_before)
        self.assertEqual(self.raw.current_stock, stock_before)
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')
        self.assertEqual(queue.delivered_quantity, Decimal('0.00'))

    def test_21_delivery_state_is_all_or_nothing(self):
        """یا همه‌چیز ثبت می‌شود یا هیچ‌چیز."""
        queue = self._build_queue(self.raw_pack4, '3')

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        queue.refresh_from_db()
        movement = StockMovement.objects.filter(
            raw_material=self.raw_pack4, movement_type='consumption'
        ).order_by('-id').first()
        # این دو باید همیشه با هم سازگار باشند
        self.assertEqual(queue.status, 'delivered')
        self.assertGreater(queue.delivered_quantity, Decimal('0'))
        self.assertIsNotNone(movement)
        self.assertGreater(movement.quantity, Decimal('0'))


class DeliveryPermissionTests(DeliveryExecutionBase):
    def test_22_unauthorized_user_rejected(self):
        queue = self._build_queue(self.raw_pack4, '3')
        stock_before = self.raw_pack4.current_stock

        self.client.force_login(self.plain_user)
        response = self.client.post(
            reverse('inventory:daily_queue_delivery', args=[queue.pk]),
            {}, **self._ajax(),
        )

        self.assertEqual(response.status_code, 403)
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')
        self.assertEqual(self.raw_pack4.current_stock, stock_before)
        self.assertEqual(
            StockMovement.objects.filter(
                raw_material=self.raw_pack4, movement_type='consumption'
            ).count(),
            0,
        )

    def test_23_anonymous_user_rejected(self):
        queue = self._build_queue(self.raw_pack4, '3')

        response = self.client.post(
            reverse('inventory:daily_queue_delivery', args=[queue.pk]),
            {}, **self._ajax(),
        )

        self.assertEqual(response.status_code, 302)
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')

    def test_24_missing_ajax_header_rejected(self):
        queue = self._build_queue(self.raw_pack4, '3')

        self.client.force_login(self.warehouse_user)
        response = self.client.post(
            reverse('inventory:daily_queue_delivery', args=[queue.pk]), {},
        )

        self.assertEqual(response.status_code, 403)
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'pending')

    def test_25_warehouse_user_can_deliver(self):
        queue = self._build_queue(self.raw_pack4, '3')

        response = self._post_delivery(queue, user=self.warehouse_user)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'])
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'delivered')

    def test_26_success_message_names_material_worker_and_amount(self):
        """
        پیام موفقیت باید به انباردار بگوید چه چیزی، به چه کسی و چقدر تحویل شد —
        نه اینکه فقط بگوید «موفق بود».
        """
        queue = self._build_queue(self.raw, '2')

        response = self._post_delivery(queue)
        payload = json.loads(response.content)
        self.assertEqual(payload['status'], 'delivered')

        stored = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(
            any(
                queue.raw_material.name in m
                and queue.worker.get_full_name() in m
                for m in stored
            ),
            stored,
        )