from django.test import TestCase
from django.contrib.auth.models import User
from decimal import Decimal
from unittest.mock import patch
import jdatetime

from product.utils import (
    is_working_day,
    consume_material_for_task,
    consume_material_for_paint_task,
)
from product.models import (
    Product, ProductCategory, ProductBOM, Part, Material,
    Order, OrderItem, Color, ProductionTask, Customer,
    PaintingProcess, PaintingStage, PaintingMaterialRequirement,
)
from inventory.models import (
    RawMaterial, RawMaterialCategory, StockMovement, MaterialIssue,
)
from inventory.views import _task_material_requirements


class IsWorkingDayTests(TestCase):
    def test_friday_is_not_working_day(self):
        friday = jdatetime.date.today()
        while friday.weekday() != 6:  # جمعه
            friday = jdatetime.date.fromgregorian(date=friday.togregorian() + __import__('datetime').timedelta(days=1))
        with patch('product.utils.Holiday.objects.filter') as mock_holiday:
            mock_holiday.return_value.exists.return_value = False
            self.assertFalse(is_working_day(friday))

    def test_saturday_is_working_day(self):
        saturday = jdatetime.date.today()
        while saturday.weekday() != 0:  # شنبه
            saturday = jdatetime.date.fromgregorian(date=saturday.togregorian() + __import__('datetime').timedelta(days=1))
        with patch('product.utils.Holiday.objects.filter') as mock_holiday:
            mock_holiday.return_value.exists.return_value = False
            self.assertTrue(is_working_day(saturday))

    def test_holiday_is_not_working_day(self):
        saturday = jdatetime.date.today()
        while saturday.weekday() != 0:
            saturday = jdatetime.date.fromgregorian(date=saturday.togregorian() + __import__('datetime').timedelta(days=1))
        with patch('product.utils.Holiday.objects.filter') as mock_holiday:
            mock_holiday.return_value.exists.return_value = True
            self.assertFalse(is_working_day(saturday))

    def test_wednesday_is_working_day(self):
        wednesday = jdatetime.date.today()
        while wednesday.weekday() != 3:  # چهارشنبه
            wednesday = jdatetime.date.fromgregorian(date=wednesday.togregorian() + __import__('datetime').timedelta(days=1))
        with patch('product.utils.Holiday.objects.filter') as mock_holiday:
            mock_holiday.return_value.exists.return_value = False
            self.assertTrue(is_working_day(wednesday))


# ============================================================
# تست‌های فرمول مصرف مواد نقاشی
# ============================================================

class PaintingMaterialRequirementTests(TestCase):
    """
    تست‌های سیستم مصرف مواد نقاشی که از PaintingMaterialRequirement استفاده می‌کند.
    این مدل از ProductBOM/Part کاملاً جدا است — مصرف رنگ/تینر/آستر بر
    اساس PaintingStage تعیین می‌شود، نه بر اساس متریال تخته‌ی زیرکار.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('testuser', password='testpass')

        cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.paint_raw = RawMaterial.objects.create(
            category=cat, name='رنگ آب‌پاشی متالیک', code='P001', unit='lit',
            min_stock_alert=Decimal('10.00'),
        )
        cls.thinner_raw = RawMaterial.objects.create(
            category=cat, name='تینر حلال', code='T001', unit='lit',
            min_stock_alert=Decimal('5.00'),
        )

        board_cat = RawMaterialCategory.objects.create(name='تخته')
        cls.board_raw = RawMaterial.objects.create(
            category=board_cat, name='MDF 16mm', code='MDF16', unit='m2',
            min_stock_alert=Decimal('0'),
        )

        cls.category = ProductCategory.objects.create(name='مبلم')
        cls.material = Material.objects.create(name='MDF', thickness=16, raw_material=cls.board_raw)
        cls.product = Product.objects.create(
            category=cls.category, name='تخت‌فرش',
            default_size='100x50', base_price=5000,
        )

        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.user, customer=cls.customer, number='ORD001',
        )

        cls.process = PaintingProcess.objects.create(
            name='رنگ متالیک', code='MET', color_codes=['8'],
        )
        cls.stage_sealer = PaintingStage.objects.create(
            process=cls.process, order=1, name='آستر',
            duration_minutes=30, drying_time_minutes=60, required_skill='painter',
        )
        cls.stage_paint = PaintingStage.objects.create(
            process=cls.process, order=2, name='رنگ رو',
            duration_minutes=45, drying_time_minutes=120, required_skill='painter',
        )

        cls.req_sealer = PaintingMaterialRequirement.objects.create(
            painting_stage=cls.stage_sealer, raw_material=cls.paint_raw,
            consumption_per_unit=Decimal('0.100'),
        )
        cls.req_paint = PaintingMaterialRequirement.objects.create(
            painting_stage=cls.stage_paint, raw_material=cls.paint_raw,
            consumption_per_unit=Decimal('0.300'),
        )
        cls.req_paint_thinner = PaintingMaterialRequirement.objects.create(
            painting_stage=cls.stage_paint, raw_material=cls.thinner_raw,
            consumption_per_unit=Decimal('0.050'),
        )

        cls.part = Part.objects.create(
            material=cls.material, name='پنل', length=100, width=50,
            pname='تخت‌فرش', routing_code='cut.cnc',
        )
        ProductBOM.objects.create(product=cls.product, part=cls.part, quantity=4)

    # --- helpers ---

    def _make_order_item(self, quantity=2):
        item = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=quantity,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=item)
        return item

    def _make_paint_task(self, item, painting_stage, quantity=2, color_part='بدنه'):
        task = ProductionTask.objects.create(
            order=item.order,
            part=None,
            station_name='paint',
            step_order=10,
            quantity=quantity,
            status='pending',
            painting_stage=painting_stage,
            order_item=item,
            color_part=color_part,
        )
        return task

    def _make_cut_task(self, quantity=4):
        task = ProductionTask.objects.create(
            order=self.order,
            part=self.part,
            station_name='cut',
            step_order=1,
            quantity=quantity,
            status='pending',
        )
        return task

    # --- تست 1: _task_material_requirements برای تسک‌های نقاشی ---

    def test_paint_task_requirements_come_from_painting_stage(self):
        """
        تسک نقاشی باید موادش از PaintingMaterialRequirement بگیرد، نه از BOM.
        برای مرحله آستر → 0.1 لیتر رنگ به ازای هر واحد؛ برای مرحله رنگ رو → 0.3 لیتر رنگ
        به ازای هر واحد.
        """
        item = self._make_order_item(quantity=3)
        task = self._make_paint_task(item, self.stage_sealer, quantity=3)

        rows = _task_material_requirements(task)
        self.assertEqual(len(rows), 1)
        raw, qty = rows[0]
        self.assertEqual(raw, self.paint_raw)
        self.assertEqual(qty, Decimal(3) * Decimal('0.100'))

    def test_paint_task_multiple_materials(self):
        """
        یک مرحله نقاشی می‌تواند چند ماده اولیه مصرف کند.
        مرحله «رنگ رو» → 0.3 لیتر رنگ + 0.05 لیتر تینر.
        """
        item = self._make_order_item(quantity=2)
        task = self._make_paint_task(item, self.stage_paint, quantity=2)

        rows = _task_material_requirements(task)
        self.assertEqual(len(rows), 2)
        by_raw = {raw: qty for raw, qty in rows}
        self.assertIn(self.paint_raw, by_raw)
        self.assertIn(self.thinner_raw, by_raw)
        self.assertEqual(by_raw[self.paint_raw], Decimal(2) * Decimal('0.300'))
        self.assertEqual(by_raw[self.thinner_raw], Decimal(2) * Decimal('0.050'))

    # --- تست 2: consume_material_for_paint_task هنگام done شدن ---

    def test_consume_material_for_paint_task_creates_stock_movements(self):
        """
        وقتی تسک نقاشی done می‌شود، یک StockMovement مصرفی برای هر
        PaintingMaterialRequirement ساخته می‌شود و idempotent است.
        """
        item = self._make_order_item(quantity=2)
        task = self._make_paint_task(item, self.stage_paint, quantity=2)
        task.status = 'done'
        task.save()

        movements = StockMovement.objects.filter(
            reference_task=task, movement_type='consumption'
        )
        self.assertEqual(movements.count(), 2)

        by_raw = {m.raw_material_id: m for m in movements}
        self.assertIn(self.paint_raw.id, by_raw)
        self.assertAlmostEqual(float(by_raw[self.paint_raw.id].quantity), 0.6, places=3)
        self.assertIn(self.thinner_raw.id, by_raw)
        self.assertAlmostEqual(float(by_raw[self.thinner_raw.id].quantity), 0.1, places=3)
        """فراخوانی مجدد با همان تسک نباید StockMovement جدید بسازد."""
        item = self._make_order_item(quantity=2)
        task = self._make_paint_task(item, self.stage_paint, quantity=2)

        consume_material_for_paint_task(task)
        consume_material_for_paint_task(task)

        movements = StockMovement.objects.filter(
            reference_task=task, movement_type='consumption'
        )
        self.assertEqual(movements.count(), 2)

    def test_consume_material_for_paint_task_no_stage(self):
        """
        تسک نقاشی بدون painting_stage (ghost task) نباید خطا بدهد
        و نباید رکورد اضافه کند.
        """
        item = self._make_order_item(quantity=2)
        task = self._make_paint_task(item, None, quantity=2)
        task.painting_stage = None
        task.save()

        result = consume_material_for_paint_task(task)
        self.assertEqual(result, [])
        self.assertEqual(
            StockMovement.objects.filter(reference_task=task).count(), 0
        )

    # --- تست 3: regression — BOM-based (cut) tasks unaffected ---

    def test_cut_task_requirements_use_bom(self):
        """
        تسک‌های ایستگاه‌های غیر از paint (مثل cut) باید همچنان از
        Part.material.raw_material استفاده کنند، نه از PaintingMaterialRequirement.
        """
        task = self._make_cut_task(quantity=4)
        rows = _task_material_requirements(task)
        self.assertEqual(len(rows), 1)
        raw, qty = rows[0]
        self.assertEqual(raw, self.board_raw)
        self.assertEqual(qty, Decimal(4) * Decimal('1'))

    def test_cut_task_consumption_unchanged(self):
        """consume_material_for_task برای تسک cut دست‌نخورته باقی می‌ماند."""
        task = self._make_cut_task(quantity=4)
        task.status = 'done'
        task.save()

        movement = StockMovement.objects.filter(
            reference_task=task, movement_type='consumption'
        ).first()
        self.assertIsNotNone(movement)
        self.assertEqual(movement.raw_material, self.board_raw)
        self.assertEqual(float(movement.quantity), 4.0)

    # --- تست 4: production_issue_queue برای تسک نقاشی ---

    def test_paint_task_appears_in_production_issue_queue_logic(self):
        """
        با صفحه‌نمایش _task_material_requirements، یک تسک نقاشی
        باید در صف انتقال مواد ظاهر شود با مقدار صحیح.
        """
        item = self._make_order_item(quantity=5)
        task = self._make_paint_task(item, self.stage_sealer, quantity=5)

        rows = _task_material_requirements(task)
        self.assertEqual(len(rows), 1)
        raw, qty = rows[0]
        self.assertEqual(raw, self.paint_raw)
        self.assertEqual(qty, Decimal(5) * Decimal('0.100'))

    # --- تست 5: auto_create_material_issues برای تسک نقاشی ---

    def test_auto_create_material_issues_for_paint_task(self):
        """
        auto_create_material_issues باید برای تسک‌های نقاشی،
        MaterialIssue با مقدار صحیح بسازد.
        """
        from product.utils import auto_create_material_issues

        item = self._make_order_item(quantity=3)
        task = self._make_paint_task(item, self.stage_sealer, quantity=3)

        result = auto_create_material_issues([task], requested_by=self.user)
        self.assertEqual(result['created'], 1)
        self.assertEqual(result['skipped'], 0)

        issue = MaterialIssue.objects.get(task=task, raw_material=self.paint_raw)
        self.assertEqual(issue.raw_material, self.paint_raw)
        self.assertEqual(float(issue.requested_quantity), 0.3)
        self.assertEqual(issue.status, 'requested')

    # --- تست 6: عدم تداخل BOM با PaintingMaterialRequirement ---

    def test_paint_task_bom_entries_ignored_for_consumption(self):
        """
        اگر یک محصول BOM قطعات فیزیکی داشته باشد، این قطعات هیچ‌گاه
        در محاسبه مصرف نقاشی استفاده نمی‌شوند.
        """
        item = self._make_order_item(quantity=2)
        task = self._make_paint_task(item, self.stage_sealer, quantity=2)

        rows = _task_material_requirements(task)
        raw_mats = [raw.id for raw, _ in rows]
        # رنگ (paint_raw) باید در لیست باشد
        self.assertIn(self.paint_raw.id, raw_mats)
        # ماده تخته (board_raw) نباید در مصرف نقاشی باشد
        self.assertNotIn(self.board_raw.id, raw_mats)

