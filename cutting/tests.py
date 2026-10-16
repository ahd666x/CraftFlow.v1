from decimal import Decimal
from django.contrib.auth.models import Group, User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from product.models import Customer, Material, Order, Part, ProductionTask
from .models import CuttingSettings, CutPlan, CutPlanSheet, SheetSizeSpec, SheetStock, SheetStockEvent
from .optimizer import make_autocut, optimize


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

    def make_plan(self):
        response = self.client.post(reverse('cutting:planner'), {'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0'})
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

    def test_multiple_standard_sizes_and_separate_offcut_batch(self):
        SheetSizeSpec.objects.create(material=self.material, name='اندازه الف', width=1800, height=3600, is_default=True)
        SheetSizeSpec.objects.create(material=self.material, name='اندازه ب', width=1220, height=2440)
        self.client.post(reverse('cutting:stock_create'), {'material': self.material.pk, 'stock_type': 'offcut', 'width': '480.5', 'height': '700.0', 'quantity': 2, 'note': 'برگشتی از کارگاه'})
        offcut = SheetStock.objects.get()
        self.assertTrue(offcut.is_offcut)
        self.assertEqual(offcut.quantity, 2)
        self.assertEqual(SheetSizeSpec.objects.filter(material=self.material).count(), 2)

    def test_plan_reserves_but_does_not_consume_stock_and_full_stays_full(self):
        full = self.add_stock()
        plan = self.make_plan()
        full.refresh_from_db()
        self.assertEqual(full.quantity, 1)
        self.assertFalse(SheetStock.objects.filter(is_offcut=True).exists())
        self.assertEqual(plan.planned_sheets.count(), 1)
        self.assertEqual(plan.planned_tasks.get().task_id, self.task.pk)
        self.assertTrue(SheetStockEvent.objects.filter(event_type='reserve', plan=plan).exists())

    def test_offcuts_first_and_full_only_do_not_delete_offcut_inventory(self):
        full = self.add_stock()
        offcut = self.add_stock(width='700', height='700', offcut=True)
        plan = self.make_plan()
        self.assertEqual(plan.planned_sheets.get().source_stock_id, offcut.pk)
        plan.status = 'cancelled'; plan.save(update_fields=['status'])
        setting = CuttingSettings.current(); setting.strategy = 'full_only'; setting.save()
        plan2 = self.make_plan()
        self.assertEqual(plan2.planned_sheets.get().source_stock_id, full.pk)
        offcut.refresh_from_db(); self.assertTrue(offcut.active); self.assertEqual(offcut.quantity, 1)

    def test_manual_priority_policy_overrides_offcut_first_when_configured(self):
        full = self.add_stock(priority=1)
        offcut = self.add_stock(width='700', height='700', offcut=True, priority=20)
        setting = CuttingSettings.current(); setting.strategy = 'priority'; setting.save()
        plan = self.make_plan()
        self.assertEqual(plan.planned_sheets.get().source_stock_id, full.pk)
        self.assertNotEqual(plan.planned_sheets.get().source_stock_id, offcut.pk)

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
        response = self.result_post(plan, **{f'rem_{sheet.pk}_1_width': '321.5', f'rem_{sheet.pk}_1_height': '211.2'})
        self.assertEqual(response.status_code, 302)
        source.refresh_from_db()
        offcut = SheetStock.objects.get(is_offcut=True)
        self.assertEqual(source.quantity, 0); self.assertFalse(source.active)
        self.assertEqual((offcut.width, offcut.height), (Decimal('321.5'), Decimal('211.2')))
        self.assertEqual(offcut.created_from_plan_id, plan.pk)
        self.assertTrue(SheetStockEvent.objects.filter(event_type='offcut', stock=offcut, plan=plan).exists())

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
        self.assertContains(self.client.get(reverse('cutting:plan_detail', args=[plan.pk])), 'این برنامه لغو شده است')
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
        self.assertEqual(self.client.get(reverse('cutting:plan_detail', args=[plan.pk])).status_code, 403)
        sheet = plan.planned_sheets.get()
        self.assertEqual(self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), {f'outcome_{sheet.pk}': 'used'}).status_code, 403)
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'planned')

    def test_inventory_and_configuration_screens_render(self):
        self.add_stock()
        for route in ('cutting:planner', 'cutting:inventory', 'cutting:full_sheets', 'cutting:offcuts', 'cutting:stock_create', 'cutting:size_specs', 'cutting:policy', 'cutting:history'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 200)

    def test_plan_detail_renders_and_downloads_the_cut_file(self):
        self.add_stock()
        plan = self.make_plan()
        response = self.client.get(reverse('cutting:plan_detail', args=[plan.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ثبت نتیجه واقعی هر ورق')
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
