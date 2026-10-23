"""Stage-1 workspace tests: TaskSpecOverride model + effective spec resolver."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from cnc_resizer.anomaly_report import find_unusual_parts
from product.models import Customer, Material, Order, Part, ProductionTask

from .models import CutPlan, CutPlanTask, SheetStock, TaskSpecOverride, TaskSpecOverrideEvent
from .specs import (
    SpecLockedError, apply_override, effective_spec, effective_specs,
    reset_override, set_excluded, spec_hash,
)


class WorkspaceFixtureMixin:
    def setUp(self):
        self.user = User.objects.create_superuser(username='ws-admin', email='ws@example.test', password='test-password')
        self.customer = Customer.objects.create(name='کارگاه فضای کاری')
        self.material = Material.objects.create(name='MDF کاری', thickness=Decimal('16.0'))
        self.material2 = Material.objects.create(name='MDF کاری ۸', thickness=Decimal('8.0'))
        self.part = Part.objects.create(
            material=self.material, name='دیواره', length=Decimal('400.0'), width=Decimal('300.0'),
            pname='کمد', routing_code='cut', turn=True, f4='PVC سفید', f3='WB-1.400x300.item9',
        )
        self.order = Order.objects.create(customer=self.customer, status='planned')
        self.task = ProductionTask.objects.create(
            order=self.order, part=self.part, station_name='cut', step_order=1,
            quantity=4, status='pending', completed_quantity=1,
        )
        self.client.force_login(self.user)


class EffectiveSpecResolverTests(WorkspaceFixtureMixin, TestCase):
    def test_no_override_returns_exact_base_values(self):
        spec = effective_spec(self.task)
        self.assertEqual(spec.length, Decimal('400.0'))
        self.assertEqual(spec.width, Decimal('300.0'))
        self.assertEqual(spec.quantity_total, 4)
        self.assertEqual(spec.quantity_completed, 1)
        self.assertEqual(spec.quantity_remaining, 3)
        self.assertEqual(spec.material_id, self.material.pk)
        self.assertTrue(spec.turn)
        self.assertEqual(spec.edges['f4'], 'PVC سفید')
        self.assertEqual(spec.overridden_fields, set())
        self.assertIsNone(spec.version)
        self.assertEqual(spec.status_code, 'ready')

    def test_override_reflects_in_spec_and_part_stays_intact(self):
        spec, events = apply_override(
            self.task, {'length': '450.5', 'width': '280', 'quantity': 6}, self.user, reason='اصلاح اندازه',
        )
        self.assertEqual(spec.length, Decimal('450.5'))
        self.assertEqual(spec.width, Decimal('280.0'))
        self.assertEqual(spec.quantity_total, 6)
        self.assertEqual(spec.quantity_remaining, 5)
        self.assertEqual(spec.overridden_fields, {'length', 'width', 'quantity'})
        self.assertEqual(spec.version, 1)
        self.part.refresh_from_db()
        self.assertEqual(self.part.length, Decimal('400.0'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.quantity, 4)
        self.assertEqual(
            set(TaskSpecOverrideEvent.objects.values_list('field', flat=True)),
            {'length', 'width', 'quantity'},
        )

    def test_field_equal_to_base_is_dropped_and_empty_override_deleted(self):
        apply_override(self.task, {'length': '400.0'}, self.user)
        self.assertFalse(TaskSpecOverride.objects.filter(task=self.task).exists())
        spec, events = apply_override(self.task, {'length': '400.0', 'width': '250'}, self.user)
        self.assertEqual([event.field for event in events], ['width'])  # length was a no-op
        self.assertEqual(spec.overridden_fields, {'width'})
        # Restating the current override value is also a no-op.
        spec_b, events_b = apply_override(self.task, {'width': '250'}, self.user)
        self.assertEqual(events_b, [])
        self.assertEqual(spec_b.version, 1)
        # Back to base → field reset to inherit, empty overlay deleted.
        spec2, events2 = apply_override(self.task, {'width': '300.0'}, self.user)
        self.assertEqual([event.field for event in events2], ['width'])
        self.assertFalse(TaskSpecOverride.objects.filter(task=self.task).exists())
        self.assertEqual(spec2.width, Decimal('300.0'))
        self.assertEqual(spec2.overridden_fields, set())

    def test_quantity_floor_is_completed_quantity(self):
        # completed=1 -> floor=1
        with self.assertRaisesRegex(ValueError, 'نمی‌تواند از مجموع'):
            apply_override(self.task, {'quantity': 0}, self.user)
        spec, _ = apply_override(self.task, {'quantity': 1}, self.user)
        self.assertEqual(spec.quantity_total, 1)
        self.assertEqual(spec.quantity_remaining, 0)

    def test_quantity_bounds_and_formats(self):
        with self.assertRaisesRegex(ValueError, 'حداکثر'):
            apply_override(self.task, {'quantity': 10001}, self.user)
        with self.assertRaisesRegex(ValueError, 'عدد صحیح'):
            apply_override(self.task, {'quantity': 'دو'}, self.user)
        with self.assertRaisesRegex(ValueError, 'یک رقم اعشار'):
            apply_override(self.task, {'length': '400.25'}, self.user)
        with self.assertRaisesRegex(ValueError, 'بزرگ‌تر از صفر'):
            apply_override(self.task, {'width': '0'}, self.user)

    def test_edge_fields_override_and_clear(self):
        spec, _ = apply_override(self.task, {'f4': 'ABS مشکی'}, self.user)
        self.assertEqual(spec.edges['f4'], 'ABS مشکی')
        spec2, _ = apply_override(self.task, {'f4': ''}, self.user)  # explicit clear
        self.assertEqual(spec2.edges['f4'], '')

    def test_material_change_updates_group(self):
        spec, _ = apply_override(self.task, {'material': self.material2.pk}, self.user)
        self.assertEqual(spec.material_id, self.material2.pk)
        self.assertEqual(spec.material_name, self.material2.name)
        self.part.refresh_from_db()
        self.assertEqual(self.part.material_id, self.material.pk)

    def test_version_increments_on_each_apply(self):
        apply_override(self.task, {'length': '410'}, self.user)
        spec, _ = apply_override(self.task, {'width': '290'}, self.user)
        self.assertEqual(spec.version, 2)
        self.assertEqual(TaskSpecOverride.objects.get(task=self.task).version, 2)

    def test_no_op_edit_keeps_version(self):
        apply_override(self.task, {'length': '410'}, self.user)
        spec, events = apply_override(self.task, {'length': '410'}, self.user)
        self.assertEqual(events, [])
        self.assertEqual(spec.version, 1)

    def test_spec_hash_stable_and_sensitive(self):
        spec = effective_spec(self.task)
        self.assertEqual(spec_hash(spec), spec_hash(effective_spec(self.task)))
        changed, _ = apply_override(self.task, {'length': '500'}, self.user)
        self.assertNotEqual(spec_hash(spec), spec_hash(changed))

    def test_dimension_edit_does_not_reset_exclusion(self):
        set_excluded(self.task, excluded=True, reason='موجود در انبار', user=self.user)
        spec, _ = apply_override(self.task, {'length': '450'}, self.user)
        self.assertTrue(spec.excluded)
        self.assertEqual(spec.excluded_reason, 'موجود در انبار')


class SpecExclusionTests(WorkspaceFixtureMixin, TestCase):
    def test_exclude_requires_reason(self):
        with self.assertRaisesRegex(ValueError, 'علت'):
            set_excluded(self.task, excluded=True, reason='  ', user=self.user)

    def test_exclude_and_include_roundtrip(self):
        spec, _ = set_excluded(self.task, excluded=True, reason='موجود در انبار', user=self.user)
        self.assertTrue(spec.excluded)
        self.assertEqual(spec.excluded_reason, 'موجود در انبار')
        self.assertEqual(spec.status_code, 'excluded')
        self.assertEqual(spec.quantity_remaining, 0)
        self.part.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.task.quantity, 4)  # order data untouched
        spec2, _ = set_excluded(self.task, excluded=False, user=self.user)
        self.assertFalse(spec2.excluded)
        self.assertEqual(spec2.quantity_remaining, 3)
        self.assertEqual(spec2.status_code, 'ready')
        self.assertFalse(TaskSpecOverride.objects.filter(task=self.task).exists())

    def test_reset_override_restores_base(self):
        apply_override(self.task, {'length': '450'}, self.user)
        spec, _ = reset_override(self.task, self.user)
        self.assertEqual(spec.length, Decimal('400.0'))
        self.assertIsNone(spec.version)
        self.assertFalse(TaskSpecOverride.objects.filter(task=self.task).exists())
        self.assertTrue(TaskSpecOverrideEvent.objects.filter(task=self.task, field='reset').exists())


class SpecPlanLockTests(WorkspaceFixtureMixin, TestCase):
    def lock_with_cut_plan(self):
        plan = CutPlan.objects.create(status='planned', plan_data={})
        CutPlanTask.objects.create(plan=plan, task=self.task, quantity=1, task_quantity=4, completed_before=1)
        return plan

    def test_cut_plan_lock_blocks_everything(self):
        plan = self.lock_with_cut_plan()
        with self.assertRaisesRegex(SpecLockedError, 'لغو کنید'):
            apply_override(self.task, {'length': '500'}, self.user)
        with self.assertRaises(SpecLockedError):
            set_excluded(self.task, excluded=True, reason='انبار', user=self.user)
        with self.assertRaises(SpecLockedError):
            reset_override(self.task, self.user)
        spec = effective_spec(self.task)
        self.assertEqual(spec.status_code, 'locked_in_plan')
        self.assertEqual(spec.reserved_plan, {'kind': 'cut', 'id': plan.pk, 'label': 'برش'})

    def test_cnc_plan_lock_blocks_everything(self):
        from cnc_resizer.models import CncSheetPlan
        stock = SheetStock.objects.create(
            material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'),
            quantity=1, created_by=self.user,
        )
        plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=stock, sheet_index=0,
            width=Decimal('1830.0'), height=Decimal('3660.0'), kerf=Decimal('4.0'),
            layout={'parts': [{'task_id': self.task.pk}]}, gcode='',
        )
        with self.assertRaisesRegex(SpecLockedError, 'CNC'):
            apply_override(self.task, {'length': '500'}, self.user)
        spec = effective_spec(self.task)
        self.assertEqual(spec.status_code, 'locked_in_plan')
        self.assertEqual(spec.reserved_plan, {'kind': 'cnc', 'id': plan.pk, 'label': 'CNC'})

    def test_cancelled_plan_does_not_lock(self):
        plan = self.lock_with_cut_plan()
        plan.status = 'cancelled'
        plan.save(update_fields=('status',))
        spec, _ = apply_override(self.task, {'length': '500'}, self.user)
        self.assertEqual(spec.length, Decimal('500.0'))
        self.assertEqual(spec.status_code, 'ready')


class CncFirstSpecRulesTests(WorkspaceFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.part.routing_code = 'cnc.cut'
        self.part.save(update_fields=('routing_code',))

    def test_status_is_needs_cnc(self):
        self.assertEqual(effective_spec(self.task).status_code, 'needs_cnc')

    def test_open_manual_issue_marks_needs_cnc_manual(self):
        from cnc_resizer.models import CncPlanningIssue
        CncPlanningIssue.objects.create(
            task=self.task, order=self.order, part=self.part, mode='cnc_sheets',
            reason='نیازمند اصلاح دستی', details={'stage': 'manual_required', 'reason_code': 'width_changed'},
        )
        spec = effective_specs([self.task])[0]
        self.assertEqual(spec.status_code, 'needs_cnc_manual')

    def test_material_change_same_thickness_allowed(self):
        material_same_thickness = Material.objects.create(name='MDF روکش', thickness=Decimal('16.0'))
        spec, _ = apply_override(self.task, {'material': material_same_thickness.pk}, self.user)
        self.assertEqual(spec.material_id, material_same_thickness.pk)

    def test_material_change_different_thickness_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ضخامت برابر'):
            apply_override(self.task, {'material': self.material2.pk}, self.user)

    def test_width_override_is_recorded_and_routes_to_manual_queue_later(self):
        # Stage-4 contract: recorded, but planning must never auto-resize width.
        spec, _ = apply_override(self.task, {'width': '350'}, self.user)
        self.assertEqual(spec.width, Decimal('350.0'))
        self.assertEqual(spec.overridden_fields, {'width'})


class EffectiveSpecsBatchTests(WorkspaceFixtureMixin, TestCase):
    def test_batch_matches_single_and_flags_statuses(self):
        tasks = [
            ProductionTask.objects.create(
                order=self.order, part=Part.objects.create(
                    material=self.material, name=f'قطعه {index}', length=Decimal('100.0'),
                    width=Decimal('200.0'), pname='کمد', routing_code='cut',
                ), station_name='cut', step_order=2, quantity=2, status='pending',
            ) for index in range(1, 4)
        ]
        tasks.append(self.task)
        apply_override(tasks[0], {'length': '150'}, self.user)
        set_excluded(tasks[1], excluded=True, reason='قبلاً تولید شده', user=self.user)
        batch = {spec.task_id: spec for spec in effective_specs(tasks)}
        for task in tasks:
            single = effective_spec(task, TaskSpecOverride.objects.filter(task=task).first())
            self.assertEqual(batch[task.pk].length, single.length)
            self.assertEqual(batch[task.pk].status_code, single.status_code)
            self.assertEqual(batch[task.pk].quantity_remaining, single.quantity_remaining)
        self.assertEqual(batch[tasks[1].pk].status_code, 'excluded')
        self.assertEqual(batch[tasks[0].pk].overridden_fields, {'length'})

    def test_batch_query_count_is_constant(self):
        for index in range(5):
            part = Part.objects.create(
                material=self.material, name=f'q{index}', length=Decimal('100.0'),
                width=Decimal('200.0'), pname='کمد', routing_code='cut',
            )
            ProductionTask.objects.create(order=self.order, part=part, station_name='cut', step_order=2, quantity=2, status='pending')
        tasks = list(ProductionTask.objects.filter(order=self.order, station_name='cut'))
        effective_specs(tasks)  # warm app caches
        # overrides + planned cut + planned cnc layouts + open manual issues
        with self.assertNumQueries(4):
            effective_specs(tasks)


class Stage2PlanningIntegrationTests(WorkspaceFixtureMixin, TestCase):
    """Stage-2 contract: the planning engines consume effective specs."""

    def setUp(self):
        super().setUp()
        self.stock = SheetStock.objects.create(
            material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'),
            quantity=2, created_by=self.user,
        )

    def make_plan(self):
        response = self.client.post(reverse('cutting:planner'), {
            'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0',
        })
        self.assertEqual(response.status_code, 302)
        return CutPlan.objects.latest('pk')

    def test_plan_uses_overridden_length_and_stores_spec_hash(self):
        from cutting.specs import effective_spec, spec_hash
        apply_override(self.task, {'length': '450'}, self.user)
        plan = self.make_plan()
        shapes = plan.plan_data['rows'][0]['shapes']
        self.assertEqual(len(shapes), 3)  # remaining = 6 - 1 completed
        self.assertEqual(Decimal(str(shapes[0]['width'])), Decimal('450'))
        self.assertEqual(Decimal(str(shapes[0]['height'])), Decimal('300'))
        row = plan.planned_tasks.get()
        self.assertEqual(row.spec_hash, spec_hash(effective_spec(self.task)))
        self.assertEqual(row.task_quantity, 4)

    def test_excluded_task_is_not_planned_but_base_data_intact(self):
        other_part = Part.objects.create(
            material=self.material, name='طبقه', length=Decimal('350.0'), width=Decimal('250.0'),
            pname='کمد', routing_code='cut',
        )
        other_task = ProductionTask.objects.create(
            order=self.order, part=other_part, station_name='cut', step_order=2,
            quantity=2, status='pending',
        )
        set_excluded(self.task, excluded=True, reason='موجود در انبار', user=self.user)
        plan = self.make_plan()
        self.assertEqual(list(plan.planned_tasks.values_list('task_id', flat=True)), [other_task.pk])
        self.task.refresh_from_db()
        self.assertEqual(self.task.quantity, 4)  # exclusion never touches the order

    def test_quantity_override_adds_pieces(self):
        apply_override(self.task, {'quantity': 6}, self.user)
        plan = self.make_plan()
        self.assertEqual(len(plan.plan_data['rows'][0]['shapes']), 5)

    def test_material_override_moves_group(self):
        SheetStock.objects.create(
            material=self.material2, width=Decimal('1220.0'), height=Decimal('2440.0'),
            quantity=1, created_by=self.user,
        )
        apply_override(self.task, {'material': self.material2.pk}, self.user)
        plan = self.make_plan()
        rows = plan.plan_data['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['type'], self.material2.name.replace(' ', '-'))
        self.assertEqual(rows[0]['thickness'], str(self.material2.thickness))
        self.assertEqual(plan.planned_sheets.first().material_id, self.material2.pk)

    def test_mark_cut_blocked_after_override_change(self):
        plan = self.make_plan()
        # Simulate an override that reached the DB outside the (now locked)
        # workspace path — e.g. through admin or a race — after planning.
        TaskSpecOverride.objects.create(task=self.task, length=Decimal('500.0'), version=1)
        sheet = plan.planned_sheets.get()
        response = self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), {
            f'outcome_{sheet.pk}': 'used',
        }, follow=True)
        self.assertContains(response, 'مشخصات قطعه پس از برنامه\u200cریزی تغییر کرده')
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'planned')  # nothing consumed
        self.task.refresh_from_db()
        self.assertEqual(self.task.completed_quantity, 1)

    def test_mark_cut_blocked_after_external_part_edit(self):
        plan = self.make_plan()
        # An admin edits the base Part directly (outside the workspace).
        self.part.length = Decimal('410.0')
        self.part.save(update_fields=('length',))
        sheet = plan.planned_sheets.get()
        response = self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), {
            f'outcome_{sheet.pk}': 'used',
        }, follow=True)
        self.assertContains(response, 'مشخصات قطعه پس از برنامه\u200cریزی تغییر کرده')

    def test_mark_cut_passes_when_spec_unchanged(self):
        plan = self.make_plan()
        sheet = plan.planned_sheets.get()
        response = self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), {
            f'outcome_{sheet.pk}': 'used',
        }, follow=True)
        self.assertContains(response, 'نتیجه واقعی برش ذخیره شد')
        self.task.refresh_from_db()
        self.assertEqual(self.task.completed_quantity, 4)

    def test_legacy_plan_without_hash_keeps_old_behaviour(self):
        plan = self.make_plan()
        plan.planned_tasks.update(spec_hash=None)  # simulate pre-workspace plan
        sheet = plan.planned_sheets.get()
        self.part.length = Decimal('410.0')
        self.part.save(update_fields=('length',))
        response = self.client.post(reverse('cutting:mark_cut', args=[plan.pk]), {
            f'outcome_{sheet.pk}': 'used',
        }, follow=True)
        self.assertContains(response, 'نتیجه واقعی برش ذخیره شد')

    def test_download_cut_blocked_after_spec_change(self):
        plan = self.make_plan()
        TaskSpecOverride.objects.create(task=self.task, length=Decimal('500.0'), version=1)
        response = self.client.get(reverse('cutting:download_cut', args=[plan.pk]))
        self.assertEqual(response.status_code, 409)
        TaskSpecOverride.objects.filter(task=self.task).delete()  # back to planned spec
        response = self.client.get(reverse('cutting:download_cut', args=[plan.pk]))
        self.assertEqual(response.status_code, 200)

    def test_preview_reflects_overridden_dimensions(self):
        apply_override(self.task, {'length': '450', 'quantity': 6}, self.user)
        response = self.client.get(reverse('cutting:planner_preview'), {'order_ids': [self.order.pk], 'kerf': '4.0'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['piece_count'], 5)
        material_row = payload['materials'][0]
        self.assertEqual(Decimal(material_row['required_area_m2']) * 1000000, Decimal('450') * Decimal('300') * 5)

    def test_cnc_xml_carries_edge_bands_from_override(self):
        apply_override(self.task, {'f4': 'ABS مشکی', 'f26': 'PVC آبی'}, self.user)
        plan = self.make_plan()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(self.client.get(reverse('cutting:download_cut', args=[plan.pk])).content)
        ns = {'a': 'http://www.King-stone.com'}
        shape = root.find('.//a:Objective/a:Shape', ns)
        self.assertEqual(shape.attrib['F4'], 'ABS مشکی')
        self.assertEqual(shape.attrib['F26'], 'PVC آبی')


class Stage2AnomalyReportTests(WorkspaceFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        SheetStock.objects.create(
            material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'),
            quantity=1, created_by=self.user,
        )
    def test_overridden_length_is_reported_against_limit(self):
        self.part.routing_code = 'cnc.cut'
        self.part.save(update_fields=('routing_code',))
        apply_override(self.task, {'length': '3500'}, self.user)
        report = find_unusual_parts(order_ids=[self.order.pk])
        self.assertEqual(len(report), 1)
        self.assertIn('از حد مجاز', report[0]['reasons'][0])
        self.assertEqual(Decimal(report[0]['length']), Decimal('3500.0'))

    def test_excluded_task_is_not_reported(self):
        self.part.routing_code = 'cnc.cut'
        self.part.save(update_fields=('routing_code',))
        apply_override(self.task, {'length': '3500'}, self.user)
        set_excluded(self.task, excluded=True, reason='انبار', user=self.user)
        self.assertEqual(find_unusual_parts(order_ids=[self.order.pk]), [])

    def test_base_values_without_override(self):
        report = find_unusual_parts(order_ids=[self.order.pk])
        self.assertEqual(report, [])


class Stage2CncStalenessTests(WorkspaceFixtureMixin, TestCase):
    """The CNC completion/download gates re-check the planning-time spec hash."""

    def setUp(self):
        super().setUp()
        self.stock = SheetStock.objects.create(
            material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'),
            quantity=2, created_by=self.user,
        )

    def plan_with_layout(self, spec_hash_value):
        from cnc_resizer.models import CncSheetPlan
        return CncSheetPlan.objects.create(
            material=self.material, source_stock=self.stock, sheet_index=0,
            width=Decimal('1830.0'), height=Decimal('3660.0'), kerf=Decimal('4.0'),
            layout={'parts': [{
                'task_id': self.task.pk, 'part_id': self.part.pk, 'order_id': self.order.pk,
                'task_quantity': 4, 'completed_before': 1,
                'spec_hash': spec_hash_value,
            }]}, gcode='G21\nM2', output_verified=True,
        )

    def test_complete_blocked_after_spec_change(self):
        from cutting.specs import effective_spec, spec_hash
        plan = self.plan_with_layout(spec_hash(effective_spec(self.task)))
        TaskSpecOverride.objects.create(task=self.task, length=Decimal('480.0'), version=1)
        response = self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {
            'outcome': 'used',
        }, follow=True)
        self.assertContains(response, 'مشخصات قطعه پس از برنامه\u200cریزی تغییر کرده')
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'planned')
        self.task.refresh_from_db()
        self.assertEqual(self.task.completed_quantity, 1)

    def test_complete_passes_when_hash_matches(self):
        from cutting.specs import effective_spec, spec_hash
        plan = self.plan_with_layout(spec_hash(effective_spec(self.task)))
        response = self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {
            'outcome': 'used',
        }, follow=True)
        self.assertContains(response, 'نتیجه برنامه CNC')
        self.task.refresh_from_db()
        self.assertEqual(self.task.completed_quantity, 2)

    def test_download_blocked_for_stale_planned_sheet(self):
        plan = self.plan_with_layout('0' * 32)  # planning-time hash no longer matches
        response = self.client.get(reverse('cnc_resizer:download_sheet_plan', args=[plan.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertIn('معتبر نیست', response.content.decode('utf-8'))

    def test_legacy_layout_without_hash_downloads(self):
        plan = self.plan_with_layout('')
        plan.layout = {'parts': [{'task_id': self.task.pk}]}  # pre-hash layout
        plan.save(update_fields=('layout',))
        response = self.client.get(reverse('cnc_resizer:download_sheet_plan', args=[plan.pk]))
        self.assertEqual(response.status_code, 200)


class WorkspaceApiBase(WorkspaceFixtureMixin):
    def setUp(self):
        super().setUp()
        self.ws_url = lambda name, **kw: reverse(f'cutting:{name}', kwargs=kw)

    def patch_spec(self, task_id, payload, method='patch'):
        import json as _json
        url = self.ws_url('ws_task_spec', task_id=task_id)
        body = _json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if method == 'patch':
            return self.client.patch(url, body, content_type='application/json')
        return self.client.post(url, body, content_type='application/json')

    def post_json(self, name, task_id, payload):
        import json as _json
        url = self.ws_url(name, task_id=task_id)
        return self.client.post(url, _json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                content_type='application/json')


class WorkspacePermissionTests(WorkspaceApiBase, TestCase):
    def test_anonymous_gets_login_redirect(self):
        self.client.logout()
        response = self.client.get(self.ws_url('ws_orders'))
        self.assertEqual(response.status_code, 302)

    def test_non_operator_gets_403(self):
        from django.contrib.auth.models import User
        outsider = User.objects.create_user(username='plain', password='pw12345')
        self.client.force_login(outsider)
        for name in ('ws_orders',):
            response = self.client.get(self.ws_url(name))
            self.assertEqual(response.status_code, 403)
        response = self.patch_spec(self.task.pk, {'version': 0, 'fields': {'length': '500'}})
        self.assertEqual(response.status_code, 403)


class WorkspaceOrdersApiTests(WorkspaceApiBase, TestCase):
    def test_order_queue_lists_orders_with_plannable_tasks(self):
        self.order.user = self.user  # the rep is Order.user
        self.order.save(update_fields=('user',))
        response = self.client.get(self.ws_url('ws_orders'))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = [row for row in payload['rows'] if row['id'] == self.order.pk]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['rep_name'], 'ws-admin')  # Order.user is the rep
        self.assertEqual(row['status'], 'planned')
        self.assertEqual(row['planning_state'], 'idle')

    def test_rep_name_prefers_order_user(self):
        from django.contrib.auth.models import User
        rep = User.objects.create_user(username='rep1', first_name='رضا', last_name='محمدی', password='pw')
        self.order.user = rep
        self.order.save(update_fields=('user',))
        payload = self.client.get(self.ws_url('ws_orders')).json()
        row = next(row for row in payload['rows'] if row['id'] == self.order.pk)
        self.assertEqual(row['rep_name'], 'رضا محمدی')

    def test_search_and_status_filters(self):
        self.order.number = '1042'
        self.order.save(update_fields=('number',))
        payload = self.client.get(self.ws_url('ws_orders'), {'q': '1042'}).json()
        self.assertEqual([row['id'] for row in payload['rows']], [self.order.pk])
        payload = self.client.get(self.ws_url('ws_orders'), {'q': str(self.order.pk)}).json()
        self.assertTrue(any(row['id'] == self.order.pk for row in payload['rows']))
        payload = self.client.get(self.ws_url('ws_orders'), {'status': 'cancelled'}).json()
        self.assertEqual(payload['rows'], [])

    def test_excluded_only_orders_still_listed(self):
        # An order whose tasks are all excluded still belongs in the queue
        # (the operator manages the exclusion from here).
        set_excluded(self.task, excluded=True, reason='موجود در انبار', user=self.user)
        payload = self.client.get(self.ws_url('ws_orders')).json()
        self.assertTrue(any(row['id'] == self.order.pk for row in payload['rows']))

    def test_planning_state_detects_lock(self):
        plan = CutPlan.objects.create(status='planned', plan_data={})
        CutPlanTask.objects.create(plan=plan, task=self.task, quantity=1, task_quantity=4, completed_before=1)
        payload = self.client.get(self.ws_url('ws_orders')).json()
        row = next(row for row in payload['rows'] if row['id'] == self.order.pk)
        self.assertEqual(row['planning_state'], 'in_plan')


class WorkspaceOrderItemsApiTests(WorkspaceApiBase, TestCase):
    def make_product(self, name='کمد دو درب'):
        from product.models import Product, ProductCategory
        category = ProductCategory.objects.create(name='دسته آزمون')
        return Product.objects.create(name=name, category=category)

    def test_items_grouping_and_materials(self):
        from product.models import OrderItem
        product = self.make_product()
        item = OrderItem.objects.create(order=self.order, product=product, quantity=2, size='200x80')
        self.task.order_item = item
        self.task.save(update_fields=('order_item',))
        payload = self.client.get(self.ws_url('ws_order', order_id=self.order.pk)).json()
        self.assertEqual(len(payload['items']), 1)
        row = payload['items'][0]
        self.assertEqual(row['product_name'], 'کمد دو درب')
        self.assertEqual(row['quantity'], 2)
        self.assertEqual(row['piece_count'], 3)  # remaining of the single task
        self.assertEqual(payload['materials'][0]['name'], self.material.name)


class WorkspacePartsApiTests(WorkspaceApiBase, TestCase):
    def seed_rows(self):
        parts = []
        for index in range(1, 4):
            part = Part.objects.create(
                material=self.material if index % 2 else self.material2,
                name=f'قطعه {index}', length=Decimal('100.0') + index,
                width=Decimal('200.0'), pname='کمد', routing_code='cut', f3=f'BC-{index}',
            )
            parts.append(ProductionTask.objects.create(
                order=self.order, part=part, station_name='cut', step_order=2,
                quantity=2, status='pending',
            ))
        return parts

    def test_rows_and_summary(self):
        tasks = self.seed_rows()
        payload = self.client.get(self.ws_url('ws_parts', order_id=self.order.pk)).json()
        self.assertEqual(payload['count'], 4)
        row = next(row for row in payload['rows'] if row['task_id'] == self.task.pk)
        self.assertEqual(row['length'], '400.0')
        self.assertEqual(row['status_code'], 'ready')
        self.assertTrue(row['editable'])
        self.assertEqual(row['version'], 0)
        self.assertEqual(row['edges']['f4'], 'PVC سفید')
        self.assertEqual(len(payload['summary']), 2)

    def test_filters_combine(self):
        tasks = self.seed_rows()
        material_m1 = self.material.pk
        payload = self.client.get(self.ws_url('ws_parts', order_id=self.order.pk),
                                  {'material': material_m1, 'q': 'قطعه'}).json()
        names = {row['name'] for row in payload['rows']}
        self.assertEqual(names, {'قطعه 1', 'قطعه 3'})  # دیواره is m1 but doesn't match q
        payload = self.client.get(self.ws_url('ws_parts', order_id=self.order.pk), {'item': 'all'}).json()
        self.assertEqual(payload['count'], 4)

    def test_status_labels_and_lock_flag(self):
        plan = CutPlan.objects.create(status='planned', plan_data={})
        CutPlanTask.objects.create(plan=plan, task=self.task, quantity=1, task_quantity=4, completed_before=1)
        payload = self.client.get(self.ws_url('ws_parts', order_id=self.order.pk)).json()
        row = next(row for row in payload['rows'] if row['task_id'] == self.task.pk)
        self.assertEqual(row['status_code'], 'locked_in_plan')
        self.assertEqual(row['status_label'], 'قفل (در برنامه)')
        self.assertFalse(row['editable'])
        self.assertEqual(row['reserved_plan']['kind'], 'cut')

    def test_row_query_count_is_constant(self):
        self.seed_rows()
        url = self.ws_url('ws_parts', order_id=self.order.pk)
        self.client.get(url)  # warm caches
        # session + user + order-exists + tasks + overrides + cut + cnc + issues
        with self.assertNumQueries(8):
            self.client.get(url)


class WorkspaceTaskEditApiTests(WorkspaceApiBase, TestCase):
    def test_patch_spec_updates_row_and_bumps_version(self):
        response = self.patch_spec(self.task.pk, {'version': 0, 'fields': {'length': '450.5', 'quantity': 6}, 'reason': 'اصلاح اندازه'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['row']['length'], '450.5')
        self.assertEqual(payload['row']['quantity_total'], 6)
        self.assertEqual(payload['row']['version'], 1)
        self.assertIn('length', payload['row']['overridden_fields'])

    def test_patch_accepts_patch_and_post(self):
        response = self.patch_spec(self.task.pk, {'version': 0, 'fields': {'width': '280'}}, method='patch')
        self.assertEqual(response.status_code, 200)
        response = self.patch_spec(self.task.pk, {'version': 1, 'fields': {'width': '290'}}, method='post')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['row']['width'], '290.0')

    def test_version_conflict_returns_409_with_fresh_row(self):
        apply_override(self.task, {'length': '410'}, self.user)
        response = self.patch_spec(self.task.pk, {'version': 0, 'fields': {'width': '280'}})
        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload['current_version'], 1)
        self.assertEqual(payload['row']['length'], '410.0')
        self.assertEqual(TaskSpecOverride.objects.get(task=self.task).width, None)

    def test_validation_error_is_400_persian(self):
        response = self.patch_spec(self.task.pk, {'version': 0, 'fields': {'length': 'abc'}})
        self.assertEqual(response.status_code, 400)
        self.assertIn('باید عددی باشد', response.json()['error'])
        response = self.patch_spec(self.task.pk, {'version': 0, 'fields': {'quantity': 0}})
        self.assertEqual(response.status_code, 400)
        self.assertIn('نمی\u200cتواند از مجموع', response.json()['error'])

    def test_locked_task_is_409(self):
        plan = CutPlan.objects.create(status='planned', plan_data={})
        CutPlanTask.objects.create(plan=plan, task=self.task, quantity=1, task_quantity=4, completed_before=1)
        response = self.patch_spec(self.task.pk, {'version': 0, 'fields': {'length': '450'}})
        self.assertEqual(response.status_code, 409)
        self.assertIn('لغو کنید', response.json()['error'])

    def test_exclude_requires_reason_then_include(self):
        response = self.post_json('ws_task_exclude', self.task.pk, {'version': 0, 'reason': '   '})
        self.assertEqual(response.status_code, 400)
        response = self.post_json('ws_task_exclude', self.task.pk, {'version': 0, 'reason': 'موجود در انبار'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['row']['excluded'])
        self.assertEqual(response.json()['row']['status_code'], 'excluded')
        response = self.post_json('ws_task_include', self.task.pk, {'version': 1})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['row']['excluded'])

    def test_reset_clears_overrides(self):
        apply_override(self.task, {'length': '450', 'width': '280'}, self.user)
        self.assertEqual(TaskSpecOverride.objects.get(task=self.task).version, 1)
        response = self.post_json('ws_task_reset', self.task.pk, {'version': 1})
        self.assertEqual(response.status_code, 200)
        row = response.json()['row']
        self.assertEqual(row['length'], '400.0')
        self.assertEqual(row['overridden_fields'], [])
        self.assertEqual(row['version'], 0)
        self.assertFalse(TaskSpecOverride.objects.filter(task=self.task).exists())

    def test_reset_version_conflict(self):
        apply_override(self.task, {'length': '450'}, self.user)
        response = self.post_json('ws_task_reset', self.task.pk, {'version': 99})
        self.assertEqual(response.status_code, 409)
