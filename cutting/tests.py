from datetime import datetime
from decimal import Decimal
from django.contrib.auth.models import Group, User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
import jdatetime
from product.models import Customer, Material, Order, Part, ProductionTask
from .models import CuttingSettings, CutPlan, CutPlanSheet, SheetSizeSpec, SheetStock, SheetStockEvent
from .algorithms import (
    build_cut_sequence, compare_engines, optimize_guillotine, optimize_skyline, run_engine,
)
from .optimizer import expected_offcuts, make_autocut, optimize
from .templatetags.cutting_extras import jalali
from cnc_resizer.models import CncPlanningIssue, CncSheetPlan


class CuttingOptimizerRegressionTests(SimpleTestCase):
    """Keep the original branch tests as regression checks for the .CUT layout."""
    def test_uses_offcut_first_and_places_rotated_part(self):
        stock = [
            {'width': 900, 'height': 700, 'is_offcut': False, 'label': 'full'},
            {'width': 650, 'height': 500, 'is_offcut': True, 'label': 'offcut'},
        ]
        parts = [{'width': 480, 'height': 620, 'orig_width': 480, 'orig_height': 620, 'turn': True, 'uid': 0, 'label': 'panel'}]
        layouts = optimize(parts, stock, kerf=4)
        self.assertEqual(len(layouts), 1)
        self.assertTrue(layouts[0]['sheet']['is_offcut'])
        self.assertTrue(layouts[0]['parts'][0]['rotated'])

    def test_impossible_piece_is_reported(self):
        with self.assertRaisesRegex(ValueError, 'موجودی کافی نیست'):
            optimize([{'width': 1000, 'height': 800, 'turn': False, 'label': 'too big'}], [{'width': 500, 'height': 500, 'is_offcut': False}], 4)

    def test_generated_cut_keeps_objectives_and_layouts(self):
        row = {'type': 'Oak-16', 'thickness': '16.0', 'shapes': [{'orig_width': 200, 'orig_height': 300, 'turn': True, 'uid': 0, 'f2': 'side'}], 'sheets': [{'sheet': {'width': 900, 'height': 700, 'label': 'sheet A'}, 'parts': [{'width': 200, 'height': 300, 'x': 0, 'y': 0, 'uid': 0, 'rotated': False}]}]}
        import xml.etree.ElementTree as ET
        root = ET.fromstring(make_autocut(1, [row]))
        ns = {'a': 'http://www.King-stone.com'}
        self.assertEqual(root.tag, '{http://www.King-stone.com}AutoCUT')
        self.assertEqual(root.find('.//a:Objective', ns).attrib['Count'], '1')
        self.assertEqual(root.find('.//a:Condition/a:Shape', ns).attrib['X'], '900')
        self.assertEqual(root.find('.//a:Layout', ns).attrib['X'], '900')
        self.assertIsNotNone(root.find('.//a:Spares', ns))

    def test_expected_offcuts_include_map_coordinates(self):
        layout = {
            'sheet': {'width': 1000, 'height': 1000},
            'parts': [{'x': 5, 'y': 5, 'width': 400, 'height': 300, 'row': 5}],
        }
        remnants = expected_offcuts(layout, kerf=4, minimum=200)
        self.assertEqual(remnants, [
            {'width': 586, 'height': 300, 'x': 409, 'y': 5},
            {'width': 990, 'height': 686, 'x': 0, 'y': 309},
        ])


class JalaliTemplateFilterTests(SimpleTestCase):
    def test_jalali_filter_formats_fixed_datetime_with_latin_digits(self):
        value = datetime(2024, 3, 20, 14, 5)
        self.assertEqual(jalali(value), '1403/01/01 14:05')
        self.assertEqual(jalali(jdatetime.date(1403, 1, 1), '%Y/%m/%d'), '1403/01/01')


class OptimizerTests(TestCase):
    def test_default_policy_uses_fitting_offcut_before_full_sheet(self):
        stock = [
            {'stock_id': 1, 'width': 1830, 'height': 3660, 'is_offcut': False, 'priority': 100},
            {'stock_id': 2, 'width': 700, 'height': 700, 'is_offcut': True, 'priority': 100},
        ]
        parts = [{'width': 500, 'height': 500, 'turn': True, 'label': 'drawer'}]
        layout = optimize(parts, stock, kerf=4, policy='offcuts_first')
        self.assertEqual(layout[0]['sheet']['stock_id'], 2)

    def test_full_only_excludes_offcuts_and_priority_is_deterministic(self):
        full = {'stock_id': 1, 'width': 1100, 'height': 1100, 'is_offcut': False, 'priority': 10}
        offcut = {'stock_id': 2, 'width': 700, 'height': 700, 'is_offcut': True, 'priority': 1}
        self.assertEqual(optimize([{'width': 500, 'height': 500, 'turn': True, 'label': 'x'}], [full], policy='full_only')[0]['sheet']['stock_id'], 1)
        self.assertEqual(optimize([{'width': 500, 'height': 500, 'turn': True, 'label': 'x'}], [full, offcut], policy='priority')[0]['sheet']['stock_id'], 2)

    def test_decimal_dimensions_are_not_rounded_and_cut_xml_is_valid(self):
        part = {'width': Decimal('312.5'), 'height': Decimal('205.2'), 'orig_width': Decimal('312.5'), 'orig_height': Decimal('205.2'), 'turn': True, 'uid': 0, 'label': 'panel'}
        layout = optimize([part], [{'stock_id': 5, 'width': Decimal('800.5'), 'height': Decimal('600.5'), 'is_offcut': True}], Decimal('4.2'))
        self.assertEqual(layout[0]['parts'][0]['width'], Decimal('312.5'))
        row = {'type': 'Oak', 'thickness': '16.0', 'shapes': [{**part, 'orig_width': '312.5', 'orig_height': '205.2'}], 'sheets': layout}
        import xml.etree.ElementTree as ET
        root = ET.fromstring(make_autocut(7, [row]))
        ns = {'a': 'http://www.King-stone.com'}
        self.assertEqual(root.find('.//a:Shape', ns).attrib['X'], '312.5')


class InventoryAndCutFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='cut-admin', email='cut@example.test', password='test-password')
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name='کارگاه آزمون')
        self.material = Material.objects.create(name='MDF گردویی تست', thickness=Decimal('16.0'))
        self.part = Part.objects.create(material=self.material, name='دیواره', length=Decimal('400.0'), width=Decimal('300.0'), pname='کمد', routing_code='cut', turn=True)
        self.order = Order.objects.create(customer=self.customer, status='planned')
        self.task = ProductionTask.objects.create(order=self.order, part=self.part, station_name='cut', step_order=1, quantity=1, status='pending')

    def add_stock(self, *, width='1830.0', height='3660.0', quantity=1, offcut=False, priority=100):
        return SheetStock.objects.create(material=self.material, width=Decimal(width), height=Decimal(height), quantity=quantity, is_offcut=offcut, priority=priority, created_by=self.user)

    def make_plan(self, policy=None):
        data = {'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0'}
        if policy: data['stock_policy'] = policy
        response = self.client.post(reverse('cutting:planner'), data)
        self.assertEqual(response.status_code, 302)
        return CutPlan.objects.latest('pk')

    def result_post(self, plan, **extra):
        sheet = plan.planned_sheets.get()
        data = {'outcome_' + str(sheet.pk): 'used', **extra}
        return self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), data)

    def test_full_stock_uses_default_spec_and_custom_stock_preserves_decimal(self):
        spec = SheetSizeSpec.objects.create(material=self.material, name='استاندارد', width=Decimal('1830.0'), height=Decimal('3660.0'), is_default=True)
        response = self.client.post(reverse('cutting:stock_create'), {'material': self.material.pk, 'stock_type': 'full', 'size_spec': spec.pk, 'quantity': 3})
        self.assertEqual(response.status_code, 302)
        full = SheetStock.objects.get()
        self.assertEqual((full.width, full.height, full.quantity), (Decimal('1830.0'), Decimal('3660.0'), 3))
        self.client.post(reverse('cutting:stock_create'), {'material': self.material.pk, 'stock_type': 'full', 'width': '1234.5', 'height': '2345.5', 'quantity': 1})
        self.assertTrue(SheetStock.objects.filter(width=Decimal('1234.5'), height=Decimal('2345.5')).exists())
        self.assertEqual(SheetSizeSpec.objects.get(pk=spec.pk).width, Decimal('1830.0'))

    def test_stock_forms_keep_invalid_posted_dimensions(self):
        response = self.client.post(reverse('cutting:stock_create'), {
            'material': self.material.pk, 'stock_type': 'offcut', 'dimension_mode': 'custom',
            'width': '10.55', 'height': '500', 'quantity': '1', 'priority': '100',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="10.55"')

        stock = self.add_stock()
        response = self.client.post(reverse('cutting:stock_edit', args=[stock.pk]), {
            'material': self.material.pk, 'width': '10.55', 'height': '3660', 'quantity': '1',
            'priority': '100', 'grain': '', 'note': '', 'change_reason': 'اصلاح آزمون',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="10.55"')

    def test_multiple_standard_sizes_and_separate_offcut_batch(self):
        SheetSizeSpec.objects.create(material=self.material, name='اندازه الف', width=1800, height=3600, is_default=True)
        SheetSizeSpec.objects.create(material=self.material, name='اندازه ب', width=1220, height=2440)
        self.client.post(reverse('cutting:stock_create'), {'material': self.material.pk, 'stock_type': 'offcut', 'width': '480.5', 'height': '700.0', 'quantity': 2, 'note': 'برگشتی از کارگاه'})
        offcut = SheetStock.objects.get()
        self.assertTrue(offcut.is_offcut)
        self.assertEqual(offcut.quantity, 2)
        self.assertEqual(SheetSizeSpec.objects.filter(material=self.material).count(), 2)

    def test_quick_offcut_capture_returns_to_same_material_form(self):
        response = self.client.post(reverse('cutting:stock_create'), {
            'material': self.material.pk, 'stock_type': 'offcut', 'width': '510.5',
            'height': '420', 'quantity': '1', 'quick_repeat': '1',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn(f'material={self.material.pk}', response.url)
        self.assertIn('stock_type=offcut', response.url)
        self.assertTrue(SheetStock.objects.filter(material=self.material, is_offcut=True, width=Decimal('510.5')).exists())
        form = self.client.get(response.url)
        self.assertEqual(form.status_code, 200)
        self.assertContains(form, 'ثبت سریع چند پرت پشت‌سرهم')

    def test_plan_reserves_but_does_not_consume_stock_and_full_stays_full(self):
        full = self.add_stock()
        plan = self.make_plan()
        full.refresh_from_db()
        self.assertEqual(full.quantity, 1)
        self.assertFalse(SheetStock.objects.filter(is_offcut=True).exists())
        self.assertEqual(plan.planned_sheets.count(), 1)
        self.assertEqual(plan.planned_tasks.get().task_id, self.task.pk)
        self.assertTrue(SheetStockEvent.objects.filter(event_type='reserve', plan=plan).exists())

    def test_cnc_reservation_is_visible_and_locks_stock_operations(self):
        stock = self.add_stock()
        CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height, kerf=Decimal('4.0'),
            layout={'parts': []}, gcode='G-code', created_by=self.user,
        )
        response = self.client.get(reverse('cutting:inventory'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CUT و CNC')
        self.assertContains(response, 'رزرو شده؛ قفل')
        self.assertEqual(response.context['reserved_count'], 1)
        blocked = self.client.post(reverse('cutting:stock_deactivate', args=[stock.pk]), {'reason': 'تست'})
        self.assertEqual(blocked.status_code, 302)
        stock.refresh_from_db()
        self.assertTrue(stock.active)

    def test_offcuts_first_and_full_first_do_not_delete_offcut_inventory(self):
        full = self.add_stock()
        offcut = self.add_stock(width='700', height='700', offcut=True)
        plan = self.make_plan()
        self.assertEqual(plan.planned_sheets.get().source_stock_id, offcut.pk)
        plan.status = 'cancelled'; plan.save(update_fields=['status'])
        plan2 = self.make_plan('full_first')
        self.assertEqual(plan2.planned_sheets.get().source_stock_id, full.pk)
        self.assertEqual(plan2.stock_policy, 'full_first')
        offcut.refresh_from_db(); self.assertTrue(offcut.active); self.assertEqual(offcut.quantity, 1)
    def test_full_first_uses_full_sheet_first_and_falls_back_to_offcut(self):
        full = self.add_stock(width='410', height='310')
        offcut = self.add_stock(width='700', height='700', offcut=True)
        plan = self.make_plan('full_first')
        self.assertEqual(plan.stock_policy, 'full_first')
        self.assertEqual(plan.planned_sheets.get().source_stock_id, full.pk)
        plan.status = 'cancelled'; plan.save(update_fields=['status'])
        self.task.quantity = 2; self.task.save(update_fields=['quantity'])
        plan2 = self.make_plan('full_first')
        used = set(plan2.planned_sheets.values_list('source_stock_id', flat=True))
        self.assertEqual(used, {full.pk, offcut.pk})
    def test_untouched_full_sheet_is_not_consumed_or_converted(self):
        stock = self.add_stock()
        plan = self.make_plan(); sheet = plan.planned_sheets.get()
        response = self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), {f'outcome_{sheet.pk}': 'untouched'})
        self.assertEqual(response.status_code, 302)
        stock.refresh_from_db(); plan.refresh_from_db(); self.task.refresh_from_db()
        self.assertEqual(stock.quantity, 1); self.assertFalse(stock.is_offcut)
        self.assertEqual(plan.status, 'partial'); self.assertEqual(self.task.status, 'pending')
        self.assertEqual(self.task.completed_quantity, 0)
        self.assertFalse(SheetStock.objects.filter(is_offcut=True).exists())

    def test_actual_remnant_only_created_after_confirm_and_can_be_corrected(self):
        source = self.add_stock()
        plan = self.make_plan(); sheet = plan.planned_sheets.get()
        self.assertFalse(SheetStock.objects.filter(is_offcut=True).exists())
        detail = self.client.get(reverse('cutting:plan_detail', args=[plan.pk]))
        self.assertContains(detail, 'placeholder="اندازه‌گیری شود"')
        self.assertNotContains(detail, f'name="rem_{sheet.pk}_1_width" value=')
        result = {f'outcome_{sheet.pk}': 'used'}
        for index in range(1, 6):
            result[f'rem_{sheet.pk}_{index}_width'] = '100'
            result[f'rem_{sheet.pk}_{index}_height'] = '100'
        response = self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), result)
        self.assertEqual(response.status_code, 302)
        source.refresh_from_db()
        offcut = SheetStock.objects.filter(is_offcut=True).order_by('pk').first()
        self.assertEqual(source.quantity, 0); self.assertFalse(source.active)
        self.assertEqual((offcut.width, offcut.height), (Decimal('100.0'), Decimal('100.0')))
        self.assertEqual(SheetStock.objects.filter(is_offcut=True).count(), 5)
        self.assertEqual(offcut.created_from_plan_id, plan.pk)
        self.assertTrue(SheetStockEvent.objects.filter(event_type='offcut', stock=offcut, plan=plan).exists())

    def test_used_sheet_cannot_be_recorded_as_a_full_sheet_remnant(self):
        source = self.add_stock()
        plan = self.make_plan(); sheet = plan.planned_sheets.get()
        response = self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), {
            f'outcome_{sheet.pk}': 'used',
            f'rem_{sheet.pk}_1_width': str(sheet.width),
            f'rem_{sheet.pk}_1_height': str(sheet.height),
        })
        self.assertEqual(response.status_code, 302)
        source.refresh_from_db(); plan.refresh_from_db()
        self.assertEqual(source.quantity, 1)
        self.assertEqual(plan.status, 'planned')
        self.assertFalse(SheetStock.objects.filter(is_offcut=True).exists())

    def test_idempotent_confirmation_and_exact_task_membership(self):
        source = self.add_stock()
        plan = self.make_plan(); sheet = plan.planned_sheets.get()
        later_task = ProductionTask.objects.create(order=self.order, part=self.part, station_name='cut', step_order=2, quantity=1, status='pending')
        # Final task counts must use the relational snapshot, not the mutable export JSON.
        plan.plan_data['rows'][0]['sheets'][0]['parts'] = []
        plan.save(update_fields=['plan_data'])
        payload = {f'outcome_{sheet.pk}': 'used', f'rem_{sheet.pk}_1_width': '350', f'rem_{sheet.pk}_1_height': '250'}
        self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), payload)
        self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), payload)
        source.refresh_from_db(); self.task.refresh_from_db(); later_task.refresh_from_db()
        self.assertEqual(source.quantity, 0)
        self.assertEqual(SheetStock.objects.filter(is_offcut=True).count(), 1)
        self.assertEqual(self.task.status, 'done')
        self.assertEqual(later_task.status, 'pending')

    def test_partial_result_completes_only_cut_pieces_and_next_plan_uses_remainder(self):
        self.task.quantity = 2; self.task.save(update_fields=['quantity'])
        stock = self.add_stock(width='410', height='310', quantity=2)
        first_plan = self.make_plan()
        first_sheets = list(first_plan.planned_sheets.order_by('layout_index'))
        self.assertEqual(len(first_sheets), 2)
        response = self.client.post(reverse('cutting:mark_cut', args=[first_plan.pk]), {
            f'outcome_{first_sheets[0].pk}': 'used',
            f'outcome_{first_sheets[1].pk}': 'untouched',
        })
        self.assertEqual(response.status_code, 302)
        first_plan.refresh_from_db(); stock.refresh_from_db(); self.task.refresh_from_db()
        self.assertEqual(first_plan.status, 'partial')
        self.assertEqual((self.task.completed_quantity, self.task.status), (1, 'pending'))
        self.assertEqual(stock.quantity, 1)

        second_plan = self.make_plan()
        self.assertEqual(len(second_plan.plan_data['rows'][0]['shapes']), 1)
        membership = second_plan.planned_tasks.get()
        self.assertEqual((membership.quantity, membership.task_quantity, membership.completed_before), (1, 2, 1))
        final_sheet = second_plan.planned_sheets.get()
        self.client.post(reverse('cutting:mark_cut', args=[second_plan.pk]), {f'outcome_{final_sheet.pk}': 'used'})
        self.task.refresh_from_db(); second_plan.refresh_from_db()
        self.assertEqual((self.task.completed_quantity, self.task.status), (2, 'done'))
        self.assertEqual(second_plan.status, 'cut')

    def test_cancel_releases_reserved_sheet_and_keeps_quantity(self):
        stock = self.add_stock(); plan = self.make_plan()
        self.client.post(reverse('cutting:cancel_plan', args=[plan.pk]))
        stock.refresh_from_db(); plan.refresh_from_db()
        self.assertEqual(stock.quantity, 1); self.assertEqual(plan.status, 'cancelled')
        self.assertEqual(plan.planned_sheets.get().outcome, 'released')
        self.assertTrue(SheetStockEvent.objects.filter(event_type='release', plan=plan).exists())
        self.assertFalse(SheetStockEvent.objects.filter(event_type='untouched', plan=plan).exists())
        self.assertContains(self.client.get(reverse('cutting:plan_detail', args=[plan.pk])), 'این برنامه لغو شده و رزروها آزاد شدند')
        self.assertEqual(self.client.post(reverse('cutting:planner'), {'action':'plan','order_ids':[self.order.pk],'kerf':'4'}).status_code, 302)

    def test_changed_task_or_invalid_actual_remainder_rolls_back(self):
        stock = self.add_stock(); plan = self.make_plan(); sheet = plan.planned_sheets.get()
        self.task.quantity = 2; self.task.save(update_fields=['quantity'])
        self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), {f'outcome_{sheet.pk}':'used', f'rem_{sheet.pk}_1_width':'200.55', f'rem_{sheet.pk}_1_height':'200'})
        stock.refresh_from_db(); plan.refresh_from_db()
        self.assertEqual(stock.quantity, 1); self.assertEqual(plan.status, 'planned')
        self.assertFalse(SheetStock.objects.filter(is_offcut=True).exists())

    def test_deactivation_requires_reason_and_keeps_history(self):
        offcut = self.add_stock(width='500', height='500', offcut=True)
        self.client.post(reverse('cutting:stock_deactivate', args=[offcut.pk]), {'reason':''})
        offcut.refresh_from_db(); self.assertTrue(offcut.active)
        self.client.post(reverse('cutting:stock_deactivate', args=[offcut.pk]), {'reason':'آسیب‌دیده'})
        offcut.refresh_from_db(); self.assertFalse(offcut.active); self.assertEqual(offcut.inactive_reason, 'آسیب‌دیده')
        self.assertTrue(SheetStockEvent.objects.filter(event_type='deactivate', stock=offcut, note='آسیب‌دیده').exists())

    def test_stock_edit_logs_reason_and_before_after_without_losing_batch(self):
        stock = self.add_stock(width='900', height='1800', quantity=3)
        another = Material.objects.create(name='MDF جایگزین تست', thickness=Decimal('18.0'))
        url = reverse('cutting:stock_edit', args=[stock.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {'material': another.pk, 'width': '910.5', 'height': '1810', 'quantity': 2, 'priority': 25, 'grain': 'افقی', 'note': 'اصلاح‌شده'})
        self.assertEqual(response.status_code, 200)
        stock.refresh_from_db()
        self.assertEqual((stock.material_id, stock.width, stock.height, stock.quantity), (self.material.pk, Decimal('900.0'), Decimal('1800.0'), 3))
        response = self.client.post(url, {'material': another.pk, 'width': '910.5', 'height': '1810', 'quantity': 2, 'priority': 25, 'grain': 'افقی', 'note': 'اصلاح‌شده', 'change_reason': 'اصلاح برچسب و شمارش'})
        self.assertEqual(response.status_code, 302)
        stock.refresh_from_db()
        self.assertEqual((stock.material_id, stock.width, stock.quantity), (another.pk, Decimal('910.5'), 2))
        event = SheetStockEvent.objects.filter(stock=stock, event_type='adjust').latest('pk')
        self.assertIn('اصلاح برچسب و شمارش', event.note)
        self.assertIn('MDF گردویی تست', event.note)

    def test_dashboard_counts_units_not_rows_and_filters_offcuts(self):
        self.add_stock(quantity=5)
        offcut = self.add_stock(width='700', height='700', offcut=True, quantity=3)
        response = self.client.get(reverse('cutting:inventory'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['full_count'], 5); self.assertEqual(response.context['offcut_count'], 3)
        response = self.client.get(reverse('cutting:offcuts'))
        self.assertEqual(list(response.context['page'].object_list), [offcut])
        response = self.client.get(reverse('cutting:inventory'), {'q':'700x700'})
        self.assertEqual(list(response.context['page'].object_list), [offcut])

    def test_inventory_separates_total_reserved_and_free_sheets_and_keeps_filters_in_pagination(self):
        self.add_stock(quantity=3)
        self.make_plan()
        response = self.client.get(reverse('cutting:inventory'))
        self.assertEqual((response.context['total_count'], response.context['reserved_count'], response.context['available_count']), (3, 1, 2))
        row = response.context['page'].object_list[0]
        self.assertEqual((row.reserved_quantity, row.available_quantity), (1, 2))
        for _ in range(40):
            self.add_stock(width='900', height='900')
        response = self.client.get(reverse('cutting:inventory'), {'q':'MDF', 'material':self.material.pk, 'thickness':'16.0', 'sort':'date', 'page':'1'})
        self.assertTrue(response.context['page'].has_next())
        for param in ('q=MDF', f'material={self.material.pk}', 'thickness=16.0', 'sort=date'):
            self.assertIn(param, response.context['pagination_query'])

    def test_planner_checkbox_queue_shows_available_task_and_remaining_piece_count(self):
        response = self.client.get(reverse('cutting:planner'), {'q': str(self.order.pk), 'status': 'planned'})
        self.assertEqual(response.status_code, 200)
        order = response.context['orders'][0]
        self.assertEqual((order.cut_task_count, order.remaining_pieces), (1, 1))
        # The queue is rendered client-side by workspace.js from the workspace
        # API; the planner page mounts it and exposes the JSON config, while
        # the planning POST form contract (order_ids) stays on the form.
        self.assertContains(response, 'id="workspace-root"')
        self.assertContains(response, 'id="ws-config"')
        self.assertContains(response, '/cutting/workspace/orders/')
        self.assertContains(response, 'id="planning-form"')
        self.assertContains(response, 'پیش‌نمایش نیاز مواد')

    def test_live_material_preview_uses_optimizer_without_reserving_or_consuming_stock(self):
        stock = self.add_stock(quantity=2)
        response = self.client.get(reverse('cutting:planner_preview'), {'order_ids':[self.order.pk], 'kerf':'4'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['piece_count'], 1)
        self.assertEqual(len(data['materials']), 1)
        material = data['materials'][0]
        self.assertTrue(material['fits'])
        self.assertTrue(material['estimate_is_exact'])
        self.assertEqual((material['available_sheets'], material['available_full_sheets'], material['available_offcut_sheets']), (2, 2, 0))
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 2)
        self.assertEqual(CutPlan.objects.count(), 0)

    def test_cnc_mode_preview_includes_cnc_only_orders(self):
        self.add_stock()
        self.part.routing_code = 'cnc.cut'
        self.part.save(update_fields=['routing_code'])
        response = self.client.get(reverse('cutting:planner_preview'), {
            'order_ids': [self.order.pk], 'planning_mode': 'cnc_sheets', 'kerf': '4',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['piece_count'], 1)
        self.assertEqual(len(data['materials']), 1)
        self.assertEqual(data['materials'][0]['piece_count'], 1)

    def test_standard_mode_preview_excludes_cnc_first_parts(self):
        self.add_stock()
        self.part.routing_code = ' cnc .paint'
        self.part.save(update_fields=['routing_code'])
        response = self.client.get(reverse('cutting:planner_preview'), {'order_ids': [self.order.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['piece_count'], 0)

    def test_cnc_mode_does_not_put_whitespace_normalized_cnc_parts_in_cut_plan(self):
        self.add_stock(quantity=2)
        self.part.routing_code = ' cnc .paint'
        self.part.save(update_fields=['routing_code'])
        response = self.client.post(reverse('cutting:planner'), {
            'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0', 'planning_mode': 'cnc_sheets',
        })
        self.assertEqual(response.status_code, 302)
        from cutting.models import CutPlanTask
        self.assertFalse(CutPlanTask.objects.filter(task=self.task).exists())

    def test_standard_planner_keeps_direct_cnc_parts_out_of_cut_in_mixed_order(self):
        stock = self.add_stock(quantity=2)
        self.part.routing_code = 'cnc.cut'
        self.part.save(update_fields=['routing_code'])
        self.task.quantity = 2
        self.task.save(update_fields=['quantity'])
        plain_part = Part.objects.create(
            material=self.material, name='قفسه معمولی', length=Decimal('350'), width=Decimal('250'),
            pname='کمد', routing_code='cut.paint', turn=True,
        )
        plain_task = ProductionTask.objects.create(
            order=self.order, part=plain_part, station_name='cut', step_order=2, quantity=1, status='pending',
        )
        CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height,
            layout={'parts': [{'task_id': self.task.pk}]}, gcode='G-code', created_by=self.user,
        )
        response = self.client.post(reverse('cutting:planner'), {
            'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0',
        })
        self.assertEqual(response.status_code, 302)
        cut_plan = CutPlan.objects.get()
        self.assertEqual(list(cut_plan.planned_tasks.values_list('task_id', flat=True)), [plain_task.pk])

    def test_cnc_result_consumes_stock_records_event_and_completes_piece(self):
        stock = self.add_stock()
        self.part.routing_code = 'cnc.cut'
        self.part.save(update_fields=['routing_code'])
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height,
            planned_area=Decimal('120000.00'),
            layout={'parts': [{'task_id': self.task.pk, 'part_id': self.part.pk, 'order_id': self.order.pk,
                               'task_quantity': self.task.quantity, 'completed_before': self.task.completed_quantity,
                               'width': 300, 'height': 400}]},
            gcode='G-code', output_verified=True, created_by=self.user,
        )
        response = self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {
            'outcome': 'used', 'note': 'test run', f'rem_{plan.pk}_1_width': '500',
            f'rem_{plan.pk}_1_height': '600',
        })
        self.assertEqual(response.status_code, 302)
        self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {
            'outcome': 'used', 'note': 'duplicate submission',
        })
        plan.refresh_from_db(); stock.refresh_from_db(); self.task.refresh_from_db()
        self.assertEqual((plan.status, plan.result_outcome), ('done', 'used'))
        self.assertEqual(stock.quantity, 0)
        self.assertEqual((self.task.completed_quantity, self.task.status), (1, 'done'))
        self.assertTrue(SheetStockEvent.objects.filter(cnc_plan=plan, event_type='consume', quantity_delta=-1).exists())
        self.assertEqual(SheetStockEvent.objects.filter(cnc_plan=plan, event_type='consume').count(), 1)
        self.assertTrue(SheetStock.objects.filter(created_from_cnc_plan=plan, is_offcut=True).exists())

    def test_unverified_cnc_output_cannot_be_downloaded_or_recorded_as_used(self):
        stock = self.add_stock()
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height, layout={'parts': []},
            gcode='legacy generic program', output_verified=False, created_by=self.user,
        )
        self.assertEqual(self.client.get(reverse('cnc_resizer:download_sheet_plan', args=[plan.pk])).status_code, 409)
        response = self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {'outcome': 'used'})
        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db(); stock.refresh_from_db()
        self.assertEqual((plan.status, stock.quantity), ('planned', 1))

    def test_cnc_used_result_rejects_task_mutated_after_planning(self):
        stock = self.add_stock()
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height, planned_area=Decimal('120000.00'),
            layout={'parts': [{'task_id': self.task.pk, 'part_id': self.part.pk, 'order_id': self.order.pk,
                               'task_quantity': 1, 'completed_before': 0, 'width': '300', 'height': '400'}]},
            gcode='validated test output', output_verified=True, created_by=self.user,
        )
        self.task.quantity = 2
        self.task.save(update_fields=['quantity'])
        self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {'outcome': 'used'})
        plan.refresh_from_db(); stock.refresh_from_db(); self.task.refresh_from_db()
        self.assertEqual((plan.status, stock.quantity), ('planned', 1))
        self.assertEqual((self.task.completed_quantity, self.task.status), (0, 'pending'))

    def test_untouched_cnc_result_releases_reservation_without_consuming_stock(self):
        stock = self.add_stock()
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height, layout={'parts': [{'task_id': self.task.pk}]},
            gcode='G-code', output_verified=True, created_by=self.user,
        )
        self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {'outcome': 'untouched'})
        plan.refresh_from_db(); stock.refresh_from_db(); self.task.refresh_from_db()
        self.assertEqual((plan.status, plan.result_outcome), ('done', 'untouched'))
        self.assertEqual(stock.quantity, 1)
        self.assertEqual(self.task.completed_quantity, 0)
        self.assertTrue(SheetStockEvent.objects.filter(cnc_plan=plan, event_type='untouched').exists())

    def test_untouched_cnc_result_does_not_require_verified_machine_output(self):
        stock = self.add_stock()
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height, layout={'parts': [{'task_id': self.task.pk}]},
            gcode='', output_verified=False, created_by=self.user,
        )
        response = self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {'outcome': 'untouched'})
        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db(); stock.refresh_from_db(); self.task.refresh_from_db()
        self.assertEqual((plan.status, plan.result_outcome), ('done', 'untouched'))
        self.assertEqual(stock.quantity, 1)
        self.assertEqual(self.task.completed_quantity, 0)
        self.assertTrue(SheetStockEvent.objects.filter(cnc_plan=plan, event_type='untouched').exists())

    def test_scrapped_cnc_result_does_not_require_verified_machine_output(self):
        stock = self.add_stock()
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height, layout={'parts': []},
            gcode='', output_verified=False, created_by=self.user,
        )
        response = self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {
            'outcome': 'scrapped', 'note': 'ورق ترک خورد',
        })
        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db(); stock.refresh_from_db()
        self.assertEqual((plan.status, plan.result_outcome), ('done', 'scrapped'))
        self.assertEqual(stock.quantity, 0)
        self.assertTrue(SheetStockEvent.objects.filter(cnc_plan=plan, event_type='scrap').exists())

    def test_cnc_used_sheet_cannot_be_recorded_as_a_full_sheet_remnant(self):
        stock = self.add_stock()
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height,
            planned_area=Decimal('120000.00'),
            layout={'parts': [{'task_id': self.task.pk, 'part_id': self.part.pk,
                               'order_id': self.order.pk, 'task_quantity': self.task.quantity,
                               'completed_before': self.task.completed_quantity,
                               'width': '300', 'height': '400'}]},
            gcode='test only', output_verified=True, created_by=self.user,
        )
        self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {
            'outcome': 'used', f'rem_{plan.pk}_1_width': str(plan.width),
            f'rem_{plan.pk}_1_height': str(plan.height),
        })
        plan.refresh_from_db(); stock.refresh_from_db()
        self.assertEqual((plan.status, stock.quantity), ('planned', 1))
        self.assertFalse(SheetStock.objects.filter(created_from_cnc_plan=plan).exists())

    def test_cnc_planning_issue_can_be_resolved_and_reopened_from_ui_route(self):
        issue = CncPlanningIssue.objects.create(
            task=self.task, order=self.order, part=self.part, reason='تست', created_by=self.user,
        )
        response = self.client.post(reverse('cnc_resizer:update_planning_issue', args=[issue.pk]), {
            'status': 'resolved', 'note': 'بررسی شد',
        })
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        self.assertEqual((issue.status, issue.resolution_note, issue.resolved_by), ('resolved', 'بررسی شد', self.user))
        self.client.post(reverse('cnc_resizer:update_planning_issue', args=[issue.pk]), {'status': 'open'})
        issue.refresh_from_db()
        self.assertEqual((issue.status, issue.resolved_by), ('open', None))

    def test_cnc_workbench_renders_result_and_issue_controls(self):
        stock = self.add_stock()
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height, layout={'parts': [{'task_id': self.task.pk}]},
            gcode='G-code', output_verified=True, created_by=self.user,
        )
        issue = CncPlanningIssue.objects.create(task=self.task, order=self.order, part=self.part, reason='بررسی', created_by=self.user)
        response = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]))
        self.assertContains(response, 'ثبت نتیجهٔ اجرا')
        self.assertContains(response, reverse('cnc_resizer:update_planning_issue', args=[issue.pk]))
        self.assertContains(response, 'نادیده‌گرفتن')

    def test_cnc_plan_download_and_cancel_are_owner_or_manager_only(self):
        stock = self.add_stock()
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=stock.width, height=stock.height, layout={'parts': []},
            gcode='G-code', output_verified=True, created_by=self.user,
        )
        operator = User.objects.create_user(username='cnc-other-operator', password='test')
        operator.groups.add(Group.objects.get_or_create(name='1')[0])
        self.client.force_login(operator)
        self.assertEqual(self.client.get(reverse('cnc_resizer:download_sheet_plan', args=[plan.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('cnc_resizer:cancel_sheet_plan', args=[plan.pk])).status_code, 403)
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'planned')

    def test_cnc_plan_admin_is_read_only_for_lifecycle_and_output_state(self):
        from django.contrib import admin
        model_admin = admin.site._registry[CncSheetPlan]
        readonly = set(model_admin.get_readonly_fields(None))
        self.assertTrue({'status', 'gcode', 'output_verified', 'layout', 'source_stock'}.issubset(readonly))
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))

    def test_part_profile_rejects_unsupported_http_methods(self):
        response = self.client.put(reverse('cnc_resizer:part_profile'), data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 405)

    def test_cnc_sheet_page_renders_cutting_navigation(self):
        response = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="cutting-nav')
        self.assertContains(response, 'خروجی ورق CNC')

    def test_history_filters_by_event_type_text_and_date_range(self):
        event = SheetStockEvent.objects.create(
            material=self.material, event_type='receive', quantity_delta=2,
            width=Decimal('1830.0'), height=Decimal('3660.0'), is_offcut=False,
            note='دریافت انبار شیفت صبح', created_by=self.user,
        )
        today = timezone.localdate().isoformat()
        response = self.client.get(reverse('cutting:history'), {
            'event_type':'receive', 'q':'شیفت صبح', 'material':self.material.pk,
            'date_from':today, 'date_to':today,
        })
        self.assertEqual(list(response.context['page'].object_list), [event])
        for param in ('event_type=receive', 'q=', 'material=', 'date_from=', 'date_to='):
            self.assertIn(param, response.context['pagination_query'])

    def test_history_accepts_jalali_dates_and_displays_jalali_timestamp(self):
        event = SheetStockEvent.objects.create(
            material=self.material, event_type='receive', quantity_delta=1,
            width=Decimal('1830.0'), height=Decimal('3660.0'), is_offcut=False,
            created_by=self.user,
        )
        local_event_date = timezone.localtime(event.created_at).date()
        jalali_today = jdatetime.date.fromgregorian(date=local_event_date).strftime('%Y/%m/%d')
        response = self.client.get(reverse('cutting:history'), {'date_from': jalali_today, 'date_to': jalali_today})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['page'].object_list), [event])
        self.assertContains(response, 'زمان شمسی')
        self.assertContains(response, jdatetime.datetime.fromgregorian(datetime=timezone.localtime(event.created_at)).strftime('%Y/%m/%d'))

    def test_stock_management_is_forbidden_to_regular_authenticated_user(self):
        normal = User.objects.create_user(username='no-stock-access', password='test')
        self.client.force_login(normal)
        self.assertEqual(self.client.get(reverse('cutting:inventory')).status_code, 403)
        self.assertEqual(self.client.post(reverse('cutting:stock_create'), {}).status_code, 403)

    def test_representative_cannot_plan_or_confirm_a_cut(self):
        self.add_stock()
        plan = self.make_plan()
        representative = User.objects.create_user(username='cut-representative', password='test')
        representative.groups.add(Group.objects.get_or_create(name='نماینده')[0])
        self.client.force_login(representative)
        self.assertEqual(self.client.get(reverse('cutting:planner')).status_code, 403)
        self.assertEqual(self.client.get(reverse('cnc_resizer:index')).status_code, 403)
        self.assertEqual(self.client.get(reverse('cutting:plan_detail', args=[plan.pk])).status_code, 403)
        sheet = plan.planned_sheets.get()
        self.assertEqual(self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), {f'outcome_{sheet.pk}': 'used'}).status_code, 403)
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'planned')

    def test_inventory_and_configuration_screens_render(self):
        self.add_stock()
        for route in ('cutting:planner', 'cutting:plans', 'cutting:inventory', 'cutting:full_sheets', 'cutting:offcuts', 'cutting:stock_create', 'cutting:size_specs', 'cutting:policy', 'cutting:history'):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'class="cutting-nav')
        plan = self.make_plan()
        # بازطراحی: نوار ایستگاه تک‌لایه جایگزین stepper شد — stepper باید حذف باشد
        self.assertNotContains(self.client.get(reverse('cutting:planner')), 'cutting-stepper')
        self.assertNotContains(self.client.get(reverse('cutting:plan_detail', args=[plan.pk])), 'cutting-stepper')

    def test_nav_hides_links_user_cannot_open(self):
        user = User.objects.create_user(username='op1', password='x')
        user.groups.add(Group.objects.get_or_create(name='1')[0])
        self.client.force_login(user)
        response = self.client.get(reverse('cutting:planner'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse('cutting:inventory'))
        self.assertNotContains(response, reverse('cutting:policy'))

    def test_inventory_sort_links_keep_filters_and_reset_page(self):
        response = self.client.get(reverse('cutting:inventory'), {
            'q': '1830', 'material': self.material.pk, 'thickness': '16.0',
            'sort': 'width', 'page': '1',
        })
        self.assertEqual(response.status_code, 200)
        link = response.context['sort_links']['width']
        self.assertIn('q=1830', link)
        self.assertIn(f'material={self.material.pk}', link)
        self.assertIn('thickness=16.0', link)
        self.assertIn('sort=-width', link)
        self.assertNotIn('page=', link)

    def test_default_kerf_rejects_extra_precision_and_prefills_planner(self):
        response = self.client.post(reverse('cutting:policy'), {'action': 'save_kerf', 'default_kerf': '4.55'})
        self.assertEqual(response.status_code, 302)
        setting = CuttingSettings.objects.get(pk=1)
        self.assertEqual(setting.default_kerf, Decimal('4.0'))

        response = self.client.post(reverse('cutting:policy'), {'action': 'save_kerf', 'default_kerf': '3.5'})
        self.assertEqual(response.status_code, 302)
        setting.refresh_from_db()
        self.assertEqual(setting.default_kerf, Decimal('3.5'))
        self.assertContains(self.client.get(reverse('cutting:planner')), 'value="3.5"')

    def test_policy_page_only_manages_default_kerf_and_planner_offers_stock_policy(self):
        response = self.client.get(reverse('cutting:policy'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="strategy"')
        self.assertNotContains(response, 'name="priority_')
        self.assertContains(response, 'name="default_kerf"')
        planner = self.client.get(reverse('cutting:planner'))
        self.assertContains(planner, 'name="stock_policy"')
        self.assertNotContains(planner, 'part-needs-title')
    def test_preview_follows_selected_stock_policy_without_reserving(self):
        self.add_stock(width='410', height='310')
        self.add_stock(width='700', height='700', offcut=True)
        for policy, label in (('full_first', 'ابتدا ورق کامل'), ('offcuts_first', 'ابتدا پرت')):
            data = self.client.get(reverse('cutting:planner_preview'), {
                'order_ids': [self.order.pk], 'stock_policy': policy,
            }).json()
            self.assertIn(label, data['strategy'])
        self.assertEqual(CutPlan.objects.count(), 0)

    def test_plan_detail_has_material_tabs_when_multiple_materials(self):
        other = Material.objects.create(name='MDF دوم', thickness=Decimal('18.0'))
        part2 = Part.objects.create(material=other, name='کف', length=Decimal('300.0'), width=Decimal('200.0'), pname='کمد', routing_code='cut', turn=True)
        ProductionTask.objects.create(order=self.order, part=part2, station_name='cut', step_order=2, quantity=1, status='pending')
        self.add_stock()
        SheetStock.objects.create(material=other, width=Decimal('1830.0'), height=Decimal('3660.0'), quantity=1, created_by=self.user)
        plan = self.make_plan()
        response = self.client.get(reverse('cutting:plan_detail', args=[plan.pk]))
        self.assertContains(response, 'data-material-tab="1"')

    def test_plan_detail_renders_and_downloads_the_cut_file(self):
        self.add_stock()
        plan = self.make_plan()
        sheet = plan.planned_sheets.get()
        response = self.client.get(reverse('cutting:plan_detail', args=[plan.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="cut-map"')
        self.assertContains(response, 'پذیرش این فایل روی کنترلر دستگاه تأیید نشده')
        self.assertContains(response, 'نتیجه واقعی این ورق')
        self.assertContains(response, f'name="outcome_{sheet.pk}"')
        self.assertContains(response, 'type="radio"')
        self.assertContains(response, '>1</text>')
        self.assertContains(response, 'بهره‌وری')
        self.assertContains(response, 'همه مصرف شدند')
        # نمای ایستگاهی جدید: نتیجه زیر لایهٔ پنهان، ریل کناری ورق‌ها، بدون شبیه‌سازی
        self.assertContains(response, 'data-dock-toggle="result"')
        self.assertContains(response, 'data-result-dock')
        self.assertContains(response, 'data-sheet-rail')  # ریل کناری جایگزین نوار قبلی/بعدی شد
        self.assertContains(response, 'data-dock-toggle="verlist"')
        self.assertNotContains(response, 'شبیه‌سازی')
        download = self.client.get(reverse('cutting:download_cut', args=[plan.pk]))
        self.assertEqual(download.status_code, 200)
        import xml.etree.ElementTree as ET
        self.assertEqual(ET.fromstring(download.content).tag, '{http://www.King-stone.com}AutoCUT')


class StockMigrationPreservationTests(TransactionTestCase):
    """A legacy inventory row survives the incremental upgrade and receives a history baseline."""
    reset_sequences = True

    def test_existing_sheet_stock_is_preserved_by_migration(self):
        target_old = [('cutting', '0001_initial')]
        executor = MigrationExecutor(connection)
        executor.migrate(target_old)
        old_apps = executor.loader.project_state(target_old).apps
        MaterialOld = old_apps.get_model('product', 'Material')
        SheetStockOld = old_apps.get_model('cutting', 'SheetStock')
        material = MaterialOld.objects.create(name='Legacy MDF', thickness=Decimal('16.0'))
        legacy = SheetStockOld.objects.create(material_id=material.pk, width=1830, height=3660, quantity=7, is_offcut=False, grain='lengthwise')
        legacy_id, material_id = legacy.pk, material.pk

        target_before_snapshot = [('cutting', '0004_partial_plan_progress')]
        executor = MigrationExecutor(connection)
        executor.migrate(target_before_snapshot)
        before_apps = executor.loader.project_state(target_before_snapshot).apps
        CustomerBefore = before_apps.get_model('product', 'Customer')
        OrderBefore = before_apps.get_model('product', 'Order')
        PartBefore = before_apps.get_model('product', 'Part')
        TaskBefore = before_apps.get_model('product', 'ProductionTask')
        PlanBefore = before_apps.get_model('cutting', 'CutPlan')
        PlanTaskBefore = before_apps.get_model('cutting', 'CutPlanTask')
        PlanSheetBefore = before_apps.get_model('cutting', 'CutPlanSheet')
        customer = CustomerBefore.objects.create(name='Legacy customer')
        order = OrderBefore.objects.create(customer_id=customer.pk, status='planned')
        part = PartBefore.objects.create(material_id=material_id, name='Legacy part', length=400, width=300, pname='Legacy product', routing_code='cut', turn=True)
        task = TaskBefore.objects.create(order_id=order.pk, part_id=part.pk, station_name='cut', step_order=1, quantity=1, status='pending')
        plan = PlanBefore.objects.create(plan_data={'rows': []})
        plan.orders.add(order)
        sheet = PlanSheetBefore.objects.create(plan_id=plan.pk, source_stock_id=legacy_id, material_id=material_id, layout_index=0, width=1830, height=3660)
        PlanTaskBefore.objects.create(plan_id=plan.pk, task_id=task.pk, quantity=1, task_quantity=1, completed_before=0)
        plan.plan_data = {'rows': [{'sheets': [{'sheet': {'plan_sheet_id': sheet.pk}, 'parts': [{'task_id': task.pk}]}]}]}
        plan.save(update_fields=('plan_data',))

        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        new_apps = executor.loader.project_state().apps
        stock = new_apps.get_model('cutting', 'SheetStock').objects.get(pk=legacy_id)
        event = new_apps.get_model('cutting', 'SheetStockEvent').objects.get(stock_id=legacy_id)
        snapshot = new_apps.get_model('cutting', 'CutPlanSheetTask').objects.get(plan_sheet_id=sheet.pk, task_id=task.pk)
        self.assertEqual((stock.material_id, stock.width, stock.height, stock.quantity, stock.is_offcut, stock.grain), (material_id, Decimal('1830.0'), Decimal('3660.0'), 7, False, 'lengthwise'))
        self.assertEqual((event.event_type, event.quantity_delta), ('receive', 7))
        self.assertEqual(snapshot.quantity, 1)


class PhaseBNestingEngineTests(SimpleTestCase):
    """فاز B — موتورهای گیلوتین/اسکایلاین، مقایسه، رگه، سربر و ترتیب برش."""

    def test_guillotine_places_all_inside_usable_area(self):
        stock = [{'stock_id': 1, 'width': 1000, 'height': 800, 'is_offcut': False}]
        parts = [
            {'width': 400, 'height': 300, 'turn': True, 'uid': 0, 'label': 'a'},
            {'width': 350, 'height': 250, 'turn': True, 'uid': 1, 'label': 'b'},
        ]
        layouts = optimize_guillotine(parts, stock, kerf=4)
        self.assertEqual(len(layouts), 1)
        placed = layouts[0]['parts']
        self.assertEqual(len(placed), 2)
        for part in placed:
            self.assertLessEqual(part['x'] + part['width'], Decimal('1000'))
            self.assertLessEqual(part['y'] + part['height'], Decimal('800'))

    def test_guillotine_and_skyline_report_missing_stock_with_reference_message(self):
        stock = [{'width': 1000, 'height': 800, 'is_offcut': False}]
        for engine in (optimize_guillotine, optimize_skyline):
            with self.assertRaisesRegex(ValueError, 'ورق قابل استفاده/موجودی کافی نیست'):
                engine([{'width': 2000, 'height': 500, 'turn': False, 'label': 'بزرگ'}], stock, kerf=4)

    def test_skyline_places_all_parts(self):
        stock = [{'stock_id': 1, 'width': 1000, 'height': 800, 'is_offcut': False}]
        parts = [
            {'width': 400, 'height': 300, 'turn': True, 'uid': 0, 'label': 'a'},
            {'width': 350, 'height': 250, 'turn': True, 'uid': 1, 'label': 'b'},
        ]
        layouts = optimize_skyline(parts, stock, kerf=4)
        self.assertEqual(len(layouts), 1)
        self.assertEqual(len(layouts[0]['parts']), 2)

    def test_compare_engines_picks_fewer_sheets(self):
        # اسکایلاین قطعهٔ c را روی سکوی b می‌گذارد؛ قفسه‌ای c را به ورق دوم می‌فرستد.
        stock = [
            {'stock_id': 1, 'width': 1000, 'height': 1000, 'is_offcut': False},
            {'stock_id': 2, 'width': 1000, 'height': 1000, 'is_offcut': False},
        ]
        parts = [
            {'width': 500, 'height': 900, 'turn': False, 'uid': 0, 'label': 'a'},
            {'width': 490, 'height': 450, 'turn': False, 'uid': 1, 'label': 'b'},
            {'width': 490, 'height': 440, 'turn': False, 'uid': 2, 'label': 'c'},
        ]
        results, chosen = compare_engines(parts, stock, kerf=4, names=['shelf', 'skyline'])
        self.assertEqual(chosen, 'skyline')
        by_name = {row['algorithm']: row for row in results}
        self.assertEqual(by_name['shelf']['sheet_count'], 2)
        self.assertEqual(by_name['skyline']['sheet_count'], 1)

    def test_compare_engines_tie_breaks_to_engine_order(self):
        stock = [{'stock_id': 1, 'width': 1000, 'height': 800, 'is_offcut': False}]
        parts = [{'width': 400, 'height': 300, 'turn': True, 'uid': 0, 'label': 'a'}]
        results, chosen = compare_engines(parts, stock, kerf=4, names=['guillotine', 'skyline'])
        self.assertEqual(chosen, 'guillotine')  # equal sheets → ENGINE_ORDER first

    def test_respect_grain_forbids_rotation(self):
        stock = [{'stock_id': 1, 'width': 900, 'height': 600, 'is_offcut': False}]
        part = {'width': 500, 'height': 610, 'turn': True, 'grain': 'طولی', 'uid': 0, 'label': 'r'}
        unrotated_part = dict(part, grain='')
        layouts = run_engine('guillotine', [unrotated_part], stock, kerf=4)
        self.assertTrue(layouts[0]['parts'][0]['rotated'])  # only fits rotated
        with self.assertRaises(ValueError):
            run_engine('guillotine', [part], stock, kerf=4, respect_grain=True)

    def test_trim_reduces_usable_height_and_appears_in_sequence(self):
        stock = [{'stock_id': 1, 'width': 1000, 'height': 800, 'is_offcut': False}]
        part = {'width': 500, 'height': 750, 'turn': False, 'uid': 0, 'label': 't'}
        with self.assertRaises(ValueError):
            run_engine('guillotine', [part], stock, kerf=4, trim=100)  # 800-100=700 < 750
        layouts = run_engine('guillotine', [part], stock, kerf=4, trim=0)
        sequence = build_cut_sequence(layouts[0], kerf=4, trim=100)
        self.assertEqual(sequence[0]['t'], 'trim')
        self.assertEqual(sequence[0]['at'], '100')
        self.assertEqual(sequence[0]['pieces'], [])

    def test_full_length_piece_fits_full_sheet(self):
        """قطعهٔ هم‌اندازهٔ ورق باید بدون حاشیهٔ پنهان جا شود (رایج‌ترین حالت کمد)."""
        stock = [{'stock_id': 1, 'width': 1830, 'height': 3660, 'is_offcut': False}]
        part = {'width': 3660, 'height': 480, 'turn': True, 'uid': 0, 'label': 'side'}
        for engine in ('shelf', 'guillotine', 'skyline'):
            layouts = run_engine(engine, [part], stock, kerf=4)
            self.assertEqual(len(layouts), 1, engine)
            placed = layouts[0]['parts'][0]
            self.assertTrue(placed['rotated'], engine)
            self.assertEqual(str(placed['height']), '3660')

    def test_shelf_engine_honours_trim(self):
        stock = [{'stock_id': 1, 'width': 1000, 'height': 800, 'is_offcut': False}]
        part = {'width': 500, 'height': 750, 'turn': False, 'uid': 0, 'label': 't'}
        with self.assertRaises(ValueError):
            run_engine('shelf', [part], stock, kerf=4, trim=100)  # 800-100=700 < 750
        layouts = run_engine('shelf', [part], stock, kerf=4, trim=0)
        self.assertEqual(len(layouts), 1)

    def test_build_cut_sequence_is_deterministic_rip_cross(self):
        layout = {
            'sheet': {'width': 1000, 'height': 800},
            'parts': [
                {'uid': 7, 'x': 0, 'y': 0, 'width': 300, 'height': 200, 'rotated': False},
                {'uid': 3, 'x': 304, 'y': 0, 'width': 200, 'height': 200, 'rotated': False},
            ],
        }
        sequence = build_cut_sequence(layout, kerf=4)
        self.assertEqual([cut['t'] for cut in sequence], ['cross', 'rip'])
        self.assertEqual(sequence[0]['pieces'], [7])
        self.assertEqual(sequence[1]['pieces'], [3, 7])
        self.assertEqual(sequence[1]['at'], '202')


class PhaseBPlanFlowTests(TestCase):
    """فاز B در جریان کامل برنامه‌ریزی — مقایسه، گزارش، ترتیب برش و خروجی .CUT."""

    def setUp(self):
        self.user = User.objects.create_superuser(username='cut-b', email='cutb@example.test', password='test-password')
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name='کارگاه فاز B')
        self.material = Material.objects.create(name='MDF فاز B', thickness=Decimal('16.0'))
        self.part = Part.objects.create(material=self.material, name='قطعه B', length=Decimal('400.0'), width=Decimal('300.0'), pname='کمد', routing_code='cut', turn=True)
        self.order = Order.objects.create(customer=self.customer, status='planned')
        self.task = ProductionTask.objects.create(order=self.order, part=self.part, station_name='cut', step_order=1, quantity=1, status='pending')
        SheetStock.objects.create(material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'), quantity=1, created_by=self.user)

    def test_plan_with_engine_comparison_stores_report_sequence_and_xml(self):
        response = self.client.post(reverse('cutting:planner'), {
            'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0',
            'algorithms': ['shelf', 'guillotine'],
        })
        self.assertEqual(response.status_code, 302)
        plan = CutPlan.objects.latest('pk')
        self.assertIn(plan.algorithm, ('shelf', 'guillotine'))
        self.assertEqual(len(plan.algorithm_report), 2)
        self.assertEqual({row['algorithm'] for row in plan.algorithm_report}, {'shelf', 'guillotine'})
        sheet = plan.planned_sheets.get()
        self.assertTrue(sheet.cut_sequence)
        self.assertEqual(sheet.cut_sequence[0]['t'], 'rip')
        detail = self.client.get(reverse('cutting:plan_detail', args=[plan.pk]))
        self.assertContains(detail, 'مقایسهٔ بهره‌وری الگوریتم‌ها')
        self.assertNotContains(detail, 'data-cut-sim')  # شبیه‌سازی حذف شده؛ ترتیب برش روی رکورد ورق می‌ماند
        self.assertContains(detail, 'الگوریتم:')
        cut_file = self.client.get(reverse('cutting:download_cut', args=[plan.pk]))
        self.assertEqual(cut_file.status_code, 200)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(cut_file.content)
        self.assertEqual(root.tag, '{http://www.King-stone.com}AutoCUT')
        self.assertIsNotNone(root.find('.//{http://www.King-stone.com}Layout'))

    def test_default_plan_without_engine_fields_keeps_shelf_behavior(self):
        response = self.client.post(reverse('cutting:planner'), {'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0'})
        self.assertEqual(response.status_code, 302)
        plan = CutPlan.objects.latest('pk')
        self.assertEqual(plan.algorithm, 'shelf')
        self.assertTrue(plan.planned_sheets.get().cut_sequence)
        detail = self.client.get(reverse('cutting:plan_detail', args=[plan.pk]))
        self.assertContains(detail, 'class="cut-map"')
        self.assertNotContains(detail, 'data-cut-sim')
        self.assertContains(detail, 'قفسه‌ای FFDH')

    def test_planner_offers_engine_choices_and_defaults_are_safe(self):
        planner = self.client.get(reverse('cutting:planner'))
        self.assertContains(planner, 'name="algorithms"')
        self.assertContains(planner, 'name="respect_grain"')
        self.assertContains(planner, 'name="trim"')
        self.assertContains(planner, 'name="stock_policy"')  # pre-existing contract stays

    def test_trim_and_grain_post_values_are_accepted_and_stored(self):
        response = self.client.post(reverse('cutting:planner'), {
            'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0',
            'algorithms': ['guillotine'], 'respect_grain': '1', 'trim': '10.0',
        })
        self.assertEqual(response.status_code, 302)
        plan = CutPlan.objects.latest('pk')
        self.assertEqual(plan.algorithm, 'guillotine')
        self.assertEqual(plan.plan_data.get('trim'), '10.0')
        self.assertTrue(plan.plan_data.get('respect_grain'))
