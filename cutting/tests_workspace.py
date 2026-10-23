"""Stage-1 workspace tests: TaskSpecOverride model + effective spec resolver."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

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
