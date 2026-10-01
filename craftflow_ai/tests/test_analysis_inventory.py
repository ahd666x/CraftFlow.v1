"""
تست تحلیل مواد (فاز ۲) — تأثیر انبار روی سفارش‌های باز.

شکاف دادهٔ اصلی که این فایل قفل می‌کند:

    product.Material.raw_material برای همهٔ ردیف‌ها NULL است.

بنابراین مسیر ``Order → BOM → Part → Material → RawMaterial`` در دادهٔ فعلی
وجود ندارد و هر پرسش «این سفارش چه کمبودی دارد؟» باید ``insufficient_data``
برگرداند — نه صفر، نه «سالم».
"""
from decimal import Decimal

from django.test import TestCase

from craftflow_ai.analysis.inventory import (
    OPEN_ISSUE_STATUSES,
    bom_mapping_coverage,
    material_impact,
    order_material_impact,
    stock_expression,
    stock_rows,
    warehouse_impact,
)
from inventory.models import (
    MaterialIssue,
    MaterialLeftover,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
)
from product.models import Material, Part, ProductBOM, ProductionTask

from .factories import (
    make_leftover,
    make_material_sheet,
    make_order,
    make_raw_material,
    make_tasks,
)


def map_all_materials(raw):
    """همهٔ متریال‌های محصول را به یک مادهٔ انبار وصل می‌کند."""
    for sheet in Material.objects.all():
        sheet.raw_material = raw
        sheet.save(update_fields=['raw_material'])


class StockFormulaTests(TestCase):
    def setUp(self):
        self.category, _ = RawMaterialCategory.objects.get_or_create(name='رنگ')
        self.raw = RawMaterial.objects.create(
            category=self.category, name='رنگ تست', unit='kg',
            pack_size=Decimal('5'), min_stock_alert=Decimal('10'),
        )

    def test_only_consumption_subtracts(self):
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='purchase',
            quantity=Decimal('100'),
        )
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='consumption',
            quantity=Decimal('30'),
        )
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='return', quantity=Decimal('5'),
        )
        row = stock_rows(RawMaterial.objects.filter(pk=self.raw.pk))[0]
        # 100 - 30 + 5 = 75
        self.assertEqual(Decimal(str(row.computed_stock)), Decimal('75.00'))

    def test_batch_stock_matches_the_model_property(self):
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='purchase', quantity=Decimal('42'),
        )
        row = stock_rows(RawMaterial.objects.filter(pk=self.raw.pk))[0]
        self.assertEqual(Decimal(str(row.computed_stock)), Decimal('42.00'))
        self.assertEqual(Decimal(str(self.raw.current_stock)), Decimal('42.00'))

    def test_material_without_movements_has_zero_stock(self):
        row = stock_rows(RawMaterial.objects.filter(pk=self.raw.pk))[0]
        self.assertEqual(Decimal(str(row.computed_stock)), Decimal('0.00'))

    def test_stock_expression_is_documented_as_the_real_rule(self):
        self.assertIn('consumption', str(stock_expression()))


class StockStateTests(TestCase):
    def setUp(self):
        self.raw = make_raw_material(name='ماده وضعیت', stock=Decimal('0'),
                                     min_alert=Decimal('5'))

    def test_out_of_stock(self):
        report = warehouse_impact(raw_material_id=self.raw.pk)
        self.assertEqual(report['materials'][0]['stock_state'], 'out_of_stock')

    def test_low_stock_uses_the_real_threshold(self):
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='purchase', quantity=Decimal('3'),
        )
        report = warehouse_impact(raw_material_id=self.raw.pk)
        self.assertEqual(report['materials'][0]['stock_state'], 'low')

    def test_ok_stock_above_threshold(self):
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='purchase', quantity=Decimal('50'),
        )
        report = warehouse_impact(raw_material_id=self.raw.pk)
        self.assertEqual(report['materials'][0]['stock_state'], 'ok')

    def test_threshold_edge_is_inclusive(self):
        """قاعدهٔ واقعی: stock <= min_stock_alert یعنی کم‌موجود."""
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='purchase', quantity=Decimal('5'),
        )
        report = warehouse_impact(raw_material_id=self.raw.pk)
        self.assertEqual(report['materials'][0]['stock_state'], 'low')


class MaterialMappingGapTests(TestCase):
    def setUp(self):
        self.order = make_order(status='producing')
        self.tasks = make_tasks(self.order, stations=('cut',), statuses=('pending',))

    def test_unmapped_materials_produce_insufficient_data(self):
        self.assertEqual(Material.objects.filter(raw_material__isnull=True).count(),
                         Material.objects.count())
        report = order_material_impact(self.order.id)
        self.assertEqual(report['status'], 'insufficient_data')
        self.assertEqual(report['reason'], 'missing_material_mapping')
        self.assertEqual(report['confidence'], 'low')

    def test_insufficient_data_never_reports_zero_shortage(self):
        report = order_material_impact(self.order.id)
        self.assertNotEqual(report['status'], 'healthy')
        bom = report['bom_requirements']
        self.assertEqual(bom['status'], 'insufficient_data')
        self.assertIn('blocked_capabilities', bom)

    def test_coverage_counts_are_reported(self):
        coverage = bom_mapping_coverage(self.order)
        self.assertEqual(coverage['material_rows_unmapped'],
                         coverage['material_rows'])
        self.assertFalse(coverage['mapping_complete'])

    def test_mapping_the_material_removes_the_gap(self):
        order = make_order(status='producing')
        ProductionTask.objects.create(
            order=order, order_item=order.items.first(), part=Part.objects.first(),
            station_name='cut', step_order=1, quantity=4, status='pending',
        )
        raw = make_raw_material(name='MDF', unit='kg', stock=Decimal('100'))
        map_all_materials(raw)

        coverage = bom_mapping_coverage(order)
        self.assertEqual(coverage['material_rows_unmapped'], 0)
        self.assertTrue(coverage['mapping_complete'])

    def test_task_without_part_keeps_mapping_incomplete(self):
        """تسک بدون قطعه نیاز ماده ندارد، پس نگاشت هنوز کامل نیست."""
        order = make_order(status='producing')
        ProductionTask.objects.create(
            order=order, order_item=order.items.first(), part=None,
            station_name='cut', step_order=1, quantity=4, status='pending',
        )
        raw = make_raw_material(name='MDF', unit='kg', stock=Decimal('100'))
        map_all_materials(raw)

        coverage = bom_mapping_coverage(order)
        self.assertEqual(coverage['material_rows_unmapped'], 0)
        self.assertGreater(coverage['nonpaint_tasks_unresolved'], 0)
        self.assertFalse(coverage['mapping_complete'])

    def test_partial_mapping_is_still_insufficient(self):
        """
        نگاشت ناقص کافی نیست: اگر حتی یکی از متریال‌های *موردنیاز* همین سفارش
        نگاشت نشده باشد، نیاز مواد قابل محاسبه نیست.

        عمداً دو متریالِ واقعاً مصرفیِ سفارش ساخته می‌شود تا «ناقص بودن» واقعاً
        رخ دهد. متریالِ نامرتبطِ دیتابیس نباید سفارش را ناقص کند، چون گزارش
        عمداً فقط به متریال‌های مصرفیِ همین سفارش محدود است.
        """
        item = self.order.items.first()
        original_material = item.product.bom.first().part.material

        second_sheet = make_material_sheet(name='MDF-2', thickness=Decimal('12.0'))
        second_part = Part.objects.create(
            material=second_sheet, name='قطعه دوم', pname='مبل سلوی',
            length=Decimal('100.0'), width=Decimal('50.0'), routing_code='cnc.prs',
        )
        ProductBOM.objects.create(product=item.product, part=second_part, quantity=1)

        # هر دو متریال باید واقعاً مصرفیِ یک تسکِ بازِ این سفارش باشند، وگرنه
        # خارج از محدودهٔ گزارش می‌افتند.
        ProductionTask.objects.create(
            order=self.order, order_item=item,
            part=item.product.bom.first().part,
            station_name='cut', step_order=1, quantity=1, status='pending',
        )
        ProductionTask.objects.create(
            order=self.order, order_item=item, part=second_part,
            station_name='cnc', step_order=2, quantity=1, status='pending',
        )

        raw = make_raw_material(name='MDF', unit='kg', stock=Decimal('100'))
        original_material.raw_material = raw
        original_material.save(update_fields=['raw_material'])

        coverage = bom_mapping_coverage(self.order)
        self.assertEqual(coverage['material_rows'], 2)
        self.assertEqual(coverage['material_rows_unmapped'], 1)
        self.assertFalse(coverage['mapping_complete'])


class OrderMaterialShortageTests(TestCase):
    """کمبود فقط از مسیر انبار و با ریاضیات واقعی build_plans گزارش می‌شود."""

    def setUp(self):
        self.raw = make_raw_material(name='رنگ کم', stock=Decimal('0'))
        self.order = make_order(status='producing')
        self.tasks = make_tasks(self.order, stations=('cut',), statuses=('pending',))

    def test_open_issue_produces_a_real_shortage(self):
        MaterialIssue.objects.create(
            task=self.tasks[0], raw_material=self.raw,
            requested_quantity=Decimal('8'), issued_quantity=Decimal('0'),
            purpose='production', status='requested',
        )
        report = order_material_impact(self.order.id)
        self.assertEqual(report['open_issue_count'], 1)
        self.assertEqual(report['shortage_count'], 1)
        shortage = report['shortages'][0]
        self.assertEqual(shortage['raw_material_id'], self.raw.pk)
        self.assertEqual(Decimal(shortage['shortage_amount']) > 0, True)

    def test_leftover_reduces_physical_requirement(self):
        """
        باقی‌ماندهٔ سالن اول مصرف می‌شود و فقط باقی‌مانده از انبار گرفته می‌شود.

        این دقیقاً همان ریاضیات ``inventory.services.build_plans`` است و در
        این تست دوباره نوشته نمی‌شود.
        """
        MaterialIssue.objects.create(
            task=self.tasks[0], raw_material=self.raw,
            requested_quantity=Decimal('4'), issued_quantity=Decimal('0'),
            purpose='production', status='requested',
        )
        make_leftover(self.raw, Decimal('2'))

        report = order_material_impact(self.order.id)
        self.assertEqual(report['shortage_count'], 1)
        shortage = report['shortages'][0]
        # نیاز ۴، از باقی‌مانده ۲، از انبار ۲، گِرد شده به بستهٔ ۴ (pack_size)
        self.assertEqual(Decimal(shortage['from_leftover']), Decimal('2.00'))
        self.assertEqual(Decimal(shortage['from_stock']), Decimal('2.00'))
        self.assertEqual(Decimal(shortage['physical_required']), Decimal('4.00'))
        self.assertEqual(shortage['packs'], 1)

    def test_enough_stock_means_no_shortage(self):
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='purchase', quantity=Decimal('100'),
        )
        MaterialIssue.objects.create(
            task=self.tasks[0], raw_material=self.raw,
            requested_quantity=Decimal('8'), issued_quantity=Decimal('0'),
            purpose='production', status='requested',
        )
        report = order_material_impact(self.order.id)
        self.assertEqual(report['shortage_count'], 0)

    def test_issued_issue_is_not_open(self):
        MaterialIssue.objects.create(
            task=self.tasks[0], raw_material=self.raw,
            requested_quantity=Decimal('8'), issued_quantity=Decimal('8'),
            purpose='production', status='issued',
        )
        report = order_material_impact(self.order.id)
        self.assertEqual(report['open_issue_count'], 0)
        self.assertEqual(report['shortage_count'], 0)

    def test_plan_logic_names_the_reused_service(self):
        report = order_material_impact(self.order.id)
        self.assertIn('build_plans', report['plan_logic'])

    def test_cancelled_issue_is_ignored(self):
        MaterialIssue.objects.create(
            task=self.tasks[0], raw_material=self.raw,
            requested_quantity=Decimal('8'), issued_quantity=Decimal('0'),
            purpose='production', status='cancelled',
        )
        report = order_material_impact(self.order.id)
        self.assertEqual(report['open_issue_count'], 0)

    def test_shortage_finding_carries_evidence_and_limitations(self):
        MaterialIssue.objects.create(
            task=self.tasks[0], raw_material=self.raw,
            requested_quantity=Decimal('8'), issued_quantity=Decimal('0'),
            purpose='production', status='requested',
        )
        report = order_material_impact(self.order.id)
        if report['findings']:
            finding = report['findings'][0]
            self.assertTrue(finding['evidence'])
            self.assertTrue(finding['limitations'])


class WarehouseImpactTests(TestCase):
    def setUp(self):
        self.raw = make_raw_material(name='رنگ انبار', stock=Decimal('0'))
        self.order = make_order(status='producing')
        self.tasks = make_tasks(self.order, stations=('cut',), statuses=('pending',))
        MaterialIssue.objects.create(
            task=self.tasks[0], raw_material=self.raw,
            requested_quantity=Decimal('6'), issued_quantity=Decimal('0'),
            purpose='production', status='requested',
        )

    def test_warehouse_scope_is_complete_despite_missing_bom_mapping(self):
        report = material_impact(raw_material_id=self.raw.pk)
        self.assertEqual(report['analysis'] if 'analysis' in report else
                         report['materials'][0]['raw_material_id'], self.raw.pk)
        row = report['materials'][0]
        self.assertEqual(row['open_issue_count'], 1)
        self.assertEqual(row['affected_open_orders'], 1)
        self.assertEqual(row['affected_open_order_ids'], [self.order.id])

    def test_order_scoped_bom_is_still_unavailable(self):
        report = material_impact(raw_material_id=self.raw.pk)
        bom = report['order_scoped_bom']
        self.assertEqual(bom['status'], 'insufficient_data')
        self.assertEqual(bom['reason'], 'missing_material_mapping')

    def test_affected_orders_use_distinct_order_ids(self):
        second = make_order(status='producing')
        tasks = make_tasks(second, stations=('cut',), statuses=('pending',))
        MaterialIssue.objects.create(
            task=tasks[0], raw_material=self.raw,
            requested_quantity=Decimal('2'), issued_quantity=Decimal('0'),
            purpose='production', status='partial',
        )
        report = material_impact(raw_material_id=self.raw.pk)
        row = report['materials'][0]
        self.assertEqual(row['open_issue_count'], 2)
        self.assertEqual(row['affected_open_orders'], 2)

    def test_quantities_are_exposed(self):
        report = material_impact(raw_material_id=self.raw.pk)
        row = report['materials'][0]
        self.assertEqual(Decimal(row['requested_quantity']), Decimal('6.00'))
        self.assertEqual(Decimal(row['remaining_quantity']), Decimal('6.00'))
        self.assertEqual(Decimal(row['issued_quantity']), Decimal('0.00'))

    def test_each_row_carries_machine_readable_evidence(self):
        report = material_impact(raw_material_id=self.raw.pk)
        for row in report['materials']:
            for evidence in row['evidence']:
                for key in ('metric', 'value', 'unit', 'source', 'query'):
                    self.assertIn(key, evidence)

    def test_result_size_is_bounded(self):
        for index in range(8):
            make_raw_material(name=f'ماده {index}', unit='kg')
        report = material_impact(limit=3)
        self.assertEqual(len(report['materials']), 3)
        self.assertTrue(report['truncated'])

    def test_consumption_is_reported(self):
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='consumption', quantity=Decimal('4'),
            reference_task=self.tasks[0],
        )
        report = material_impact(raw_material_id=self.raw.pk)
        row = report['materials'][0]
        self.assertEqual(row['consumption_movements'], 1)
        self.assertEqual(Decimal(row['consumption_quantity']), Decimal('4.00'))
        self.assertEqual(row['consuming_orders'], 1)

    def test_paint_consumption_reference_order_item_counts_order(self):
        """مصرف نقاشی که reference_task ندارد باید از order_item انتساب شود."""
        StockMovement.objects.create(
            raw_material=self.raw,
            movement_type='consumption',
            quantity=Decimal('2'),
            reference_order_item=self.order.items.first(),
        )
        report = material_impact(raw_material_id=self.raw.pk)
        row = report['materials'][0]
        self.assertEqual(row['consumption_movements'], 1)
        self.assertEqual(row['consuming_orders'], 1)


class MaterialImpactEdgeTests(TestCase):
    def test_unknown_raw_material_returns_none(self):
        self.assertIsNone(material_impact(raw_material_id=999999))
        self.assertIsNone(order_material_impact(999999))
        self.assertIsNone(order_material_impact('not-a-number'))
        self.assertIsNone(order_material_impact(None))

    def test_order_with_no_tasks_is_not_started(self):
        order = make_order(status='planned')
        report = order_material_impact(order.id)
        self.assertEqual(report['open_issue_count'], 0)
        self.assertEqual(report['shortage_count'], 0)

    def test_limit_is_clamped(self):
        for index in range(3):
            make_raw_material(name=f'ماده محدود {index}', unit='kg')
        self.assertLessEqual(len(warehouse_impact(limit=1)['materials']), 1)
        self.assertGreaterEqual(len(warehouse_impact(limit=999)['materials']), 1)

    def test_empty_warehouse_reports_insufficient_data(self):
        self.assertEqual(RawMaterial.objects.count(), 0)
        report = warehouse_impact(limit=10)
        self.assertEqual(report['report_status'], 'insufficient_data')
        self.assertEqual(report['confidence'], 'low')
        self.assertEqual(report['materials'], [])

    def test_open_issue_statuses_match_the_real_model(self):
        self.assertEqual(OPEN_ISSUE_STATUSES, ('requested', 'partial'))

    def test_material_leftover_is_a_single_pool_per_raw_material(self):
        """باقی‌ماندهٔ سالن یک استخر است، نه به‌ازای هر کارگر."""
        self.assertEqual(len(MaterialLeftover._meta.constraints), 0)
        raw = make_raw_material(name='ماده باقی‌مانده')
        make_leftover(raw, Decimal('5'))
        self.assertEqual(MaterialLeftover.objects.filter(raw_material=raw).count(), 1)