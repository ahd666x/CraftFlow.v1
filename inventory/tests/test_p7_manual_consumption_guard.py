"""
P7 — گارد مصرف دستی در برابر دوباره‌مصرفی تحویل صف روزانه.

هدف: هنگامی که نیاز نقاشی عادیِ یک تسک برای یک ماده از
``DailyMaterialQueue`` تحویل شده است (بستهٔ فیزیکی از انبار
خارج شده)، مصرف دستی ``scan_packaging_unit`` (W13) نباید
همان نیاز را دوبار ثبت کند.

اصول:
* صرفاً وجود صف برای یک ماده در یک روز، مصرف دستی معتبر
  را بلاک نمی‌کند — ارتباط قطعی (تسک نقاشی + همان ماده +
  صف تحویل‌شده) لازم است.
* مصرف دستی بدون ``task_id`` هیچ پیوند قابل اثباتی با صف
  ندارد و بلاک نمی‌شود.
* Rework (Engine A) و ایستگاه‌های غیرنقاشی دست‌نخورده‌اند.
* ``stock_movement_create`` (W12) هیچ پیوندی با تسک/صف ندارد
  (فقط note) — رفتارش بدون تغییر می‌ماند.
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory import services
from inventory.models import (
    DailyMaterialQueue,
    MaterialIssue,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
)
from product.models import (
    Color,
    Customer,
    Order,
    OrderItem,
    PackagingUnit,
    PaintingMaterialRequirement,
    PaintingProcess,
    PaintingStage,
    Product,
    ProductCategory,
    ProductionDefect,
    ProductionTask,
)


class P7Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        # مدیر/انباردار: superuser → is_warehouse_user True
        cls.manager = User.objects.create_superuser('p7wh', password='pw')
        cls.painter = User.objects.create_user('p7painter', password='pw')

        cls.category = RawMaterialCategory.objects.create(name='مواد P7')
        cls.raw = RawMaterial.objects.create(
            category=cls.category, name='رنگ P7', code='P7-A',
            unit='kg', pack_size=Decimal('0'),
        )
        # مادهٔ دوم — برای سناریوی «نیاز مستقل»
        cls.raw_b = RawMaterial.objects.create(
            category=cls.category, name='رنگ P7 ب', code='P7-B',
            unit='kg', pack_size=Decimal('0'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته P7')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول P7', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند P7', code='P7', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='مرحله ۱ P7',
            duration_minutes=30, drying_time_minutes=0,
        )
        # مصرف ۱ کیلو به ازای هر واحد
        PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.raw, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('1.000'),
        )

        cls.customer = Customer.objects.create(name='مشتری P7', phone='09120000007')
        cls.day = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.day, time(8, 0))
        )

    def _item(self):
        order = Order.objects.create(
            user=self.manager, customer=self.customer,
            number=f'P7-{Order.objects.count() + 1}',
        )
        item = OrderItem.objects.create(
            order=order, product=self.product, quantity=1,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=item)
        # OrderItem.sync_packaging_units یک واحد بسته‌بندی
        # با شمارهٔ ۱ می‌سازد؛ اگر سیگنالی خودکار نساخت،
        # اینجا می‌سازیم.
        if not item.packaging_units.exists():
            PackagingUnit.objects.create(
                order_item=item, unit_number=1,
                is_packed=False, is_shipped=False,
            )
        return item

    def _paint_task(self, item):
        return ProductionTask.objects.create(
            order=item.order, order_item=item, station_name='paint',
            step_order=1, quantity=1, status='pending',
            painting_stage=self.stage, color_part='بدنه',
            assigned_worker=self.painter, scheduled_start=self.day_start,
        )

    def _queue(self):
        return DailyMaterialQueue.objects.get(
            work_date=self.day, worker=self.painter, raw_material=self.raw,
        )

    def _fund_stock(self, material=None, quantity='100'):
        material = material or self.raw
        StockMovement.objects.create(
            raw_material=material, movement_type='purchase',
            quantity=Decimal(quantity), created_by=self.manager,
        )
        material.refresh_from_db()

    def _post_manual(self, item, raw, quantity, task=None):
        unit = item.packaging_units.get(unit_number=1)
        data = {
            'raw_material_id': raw.pk,
            'quantity': quantity,
        }
        if task is not None:
            data['task_id'] = task.pk
        self.client.force_login(self.manager)
        return self.client.post(
            reverse('scan_packaging_unit', args=[unit.pk]), data)

    def _consumption_count(self, material=None):
        return StockMovement.objects.filter(
            movement_type='consumption',
            **({'raw_material': material} if material is not None else {}),
        ).count()


class ManualConsumptionGuardTests(P7Base):
    def test_01_queue_delivery_manual_duplicate_is_blocked(self):
        """تحویل صف → مصرف دستی تکراری همان نیاز → باید بلاک شود."""
        item = self._item()
        task = self._paint_task(item)
        self._fund_stock()
        queue = self._queue()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)
        self.assertEqual(self._consumption_count(self.raw), 1)
        stock_after_delivery = self.raw.current_stock

        response = self._post_manual(item, self.raw, '1', task=task)

        self.assertEqual(response.status_code, 302)
        # هیچ حرکت مصرف جدیدی ساخته نشده و موجودی دوبار کم نشده.
        self.assertEqual(self._consumption_count(self.raw), 1)
        self.assertEqual(self.raw.current_stock, stock_after_delivery)

    def test_02_delivered_queue_different_material_not_blocked(self):
        """صف برای مادهٔ الف تحویل شده؛ مصرف دستی مادهٔ ب (نیاز
        مستقل) باید بلاک نشود — ارتباط قطعی با تحویل صف نیست."""
        item = self._item()
        task = self._paint_task(item)
        self._fund_stock()
        self._fund_stock(material=self.raw_b)
        queue = self._queue()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)
        stock_b_before = self.raw_b.current_stock

        response = self._post_manual(item, self.raw_b, '1', task=task)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._consumption_count(self.raw_b), 1)
        self.assertEqual(self.raw_b.current_stock, stock_b_before - 1)

    def test_03_pending_queue_does_not_block_manual(self):
        """صف وجود دارد ولی هنوز تحویل نشده — هیچ نیازی
        از انبار خارج نشده، پس مصرف دستی معتبر
        نباید بلاک شود."""
        item = self._item()
        task = self._paint_task(item)
        self._fund_stock()
        queue = self._queue()
        self.assertEqual(queue.delivered_quantity, Decimal('0'))

        response = self._post_manual(item, self.raw, '1', task=task)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._consumption_count(self.raw), 1)

    def test_04_rework_engine_a_unchanged(self):
        """Rework از مسیر Engine A (MaterialIssue → تحویل انبار)
        دست‌نخورده می‌ماند."""
        item = self._item()
        self._paint_task(item)
        defect = ProductionDefect.objects.create(
            order=item.order, order_item=item, color_part='بدنه',
            quantity=1, description='خط P7', reported_by=self.manager,
        )
        issue = MaterialIssue.objects.create(
            order_item=item, defect=defect, raw_material=self.raw,
            requested_quantity=Decimal('1.00'), purpose='rework',
            status='requested', requested_by=self.manager,
            color_part='بدنه',
        )
        self._fund_stock()
        stock_before = self.raw.current_stock

        services.execute_handover(
            issued_by=self.manager, received_by=self.painter,
            items=[(issue.pk, Decimal('1.00'))],
        )

        movement = StockMovement.objects.filter(
            movement_type='consumption', fulfilled_issue=issue).get()
        self.assertEqual(movement.quantity, Decimal('1.00'))
        self.assertEqual(self.raw.current_stock, stock_before - 1)

    def test_05_non_paint_manual_consumption_allowed(self):
        """تسک غیرنقاشی: مصرف دستی معتبر حفظ می‌شود — حتی با
        task_id، گارد شامل تسک‌های غیرنقاشی نمی‌شود."""
        item = self._item()
        cnc_task = ProductionTask.objects.create(
            order=item.order, order_item=item, station_name='cnc',
            step_order=1, quantity=1, status='pending',
            scheduled_start=self.day_start,
        )
        self._fund_stock()

        response = self._post_manual(item, self.raw, '2', task=cnc_task)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._consumption_count(self.raw), 1)
        movement = StockMovement.objects.filter(
            movement_type='consumption').get()
        self.assertEqual(movement.reference_task, cnc_task)

    def test_06_manual_without_task_id_not_blocked(self):
        """مصرف دستی بدون task_id هیچ پیوند قابل اثباتی با صف
        تحویل‌شده ندارد — با آنکه صف تحویل شده، بلاک نمی‌شود
        (حدس نمی‌زنیم)."""
        item = self._item()
        self._paint_task(item)
        self._fund_stock()
        queue = self._queue()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)
        stock_after_delivery = self.raw.current_stock

        response = self._post_manual(item, self.raw, '1', task=None)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._consumption_count(self.raw), 2)
        self.assertEqual(self.raw.current_stock, stock_after_delivery - 1)

    def test_07_queue_delivery_without_manual_unchanged(self):
        """تحویل صف بدون مصرف دستی: مسیر عادی دقیقاً همان‌طور
        که بود کار می‌کند."""
        item = self._item()
        self._paint_task(item)
        self._fund_stock()
        queue = self._queue()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)

        self.assertEqual(self._consumption_count(self.raw), 1)
        movement = StockMovement.objects.filter(
            movement_type='consumption').get()
        self.assertIsNone(movement.reference_task)
        self.assertIsNone(movement.fulfilled_issue)
        self.assertEqual(movement.quantity, Decimal('1.00'))
        queue.refresh_from_db()
        self.assertEqual(queue.status, 'delivered')
        self.assertEqual(queue.actual_consumption, Decimal('1.00'))

    def test_08_repeated_operations_keep_idempotency(self):
        """اجرای دوباره همان عملیات: تحویل دوبارهٔ صف رد می‌شود و
        گارد مصرف دستی هم پایدار است."""
        item = self._item()
        task = self._paint_task(item)
        self._fund_stock()
        queue = self._queue()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)

        # تحویل دوبارهٔ صف: رد می‌شود و حرکت جدید نمی‌سازد.
        with self.assertRaises(services.HandoverError):
            services.execute_daily_delivery(
                queue_id=queue.pk, delivered_by=self.manager)
        self.assertEqual(self._consumption_count(self.raw), 1)

        # تکرار مصرف دستی بلاک‌شده: همچنان بلاک است و حرکت نمی‌سازد.
        for _ in range(2):
            response = self._post_manual(item, self.raw, '1', task=task)
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self._consumption_count(self.raw), 1)

    def test_09_w12_manual_movement_entry_unchanged(self):
        """``stock_movement_create`` (W12) هیچ پیوندی با تسک/صف ندارد
        (فقط note) — با وجود صف تحویل‌شده، رفتار فعلی حفظ می‌شود."""
        item = self._item()
        self._paint_task(item)
        self._fund_stock()
        queue = self._queue()

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)
        stock_after_delivery = self.raw.current_stock

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse('inventory:movement_create'),
            {
                'raw_material': self.raw.pk,
                'movement_type': 'consumption',
                'quantity': '1',
                'note': 'ورود دستی P7',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        # W12 بدون گارد می‌ماند: مصرف ثبت می‌شود (ریسک شناخته‌شدهٔ
        # ورود آزاد — بدون پیوند قابل اثبات، نمی‌توان بلاک کرد).
        self.assertEqual(self._consumption_count(self.raw), 2)
        self.assertEqual(
            self.raw.current_stock, stock_after_delivery - 1)


class PaintNeedDeliveredByQueueTests(P7Base):
    def test_10_helper_read_only_and_exact(self):
        """کمک‌کنندهٔ سرویس: خواندنی، دقیق، و برای تسک غیرنقاشی
        False می‌گرداند."""
        item = self._item()
        task = self._paint_task(item)
        self._fund_stock()
        queue = self._queue()

        # هنوز تحویل نشده
        self.assertFalse(
            services.is_paint_need_delivered_by_queue(task, self.raw))

        services.execute_daily_delivery(
            queue_id=queue.pk, delivered_by=self.manager)
        self.assertTrue(
            services.is_paint_need_delivered_by_queue(task, self.raw))
        # مادهٔ دیگر → False
        self.assertFalse(
            services.is_paint_need_delivered_by_queue(task, self.raw_b))
        # ورودی‌های نامعتبر → False
        self.assertFalse(
            services.is_paint_need_delivered_by_queue(None, self.raw))
        self.assertFalse(
            services.is_paint_need_delivered_by_queue(task, None))

    def test_11_helper_false_for_non_paint_task(self):
        item = self._item()
        task = ProductionTask.objects.create(
            order=item.order, order_item=item, station_name='cnc',
            step_order=1, quantity=1, status='pending',
            scheduled_start=self.day_start,
        )
        self._fund_stock()
        self.assertFalse(
            services.is_paint_need_delivered_by_queue(task, self.raw))
