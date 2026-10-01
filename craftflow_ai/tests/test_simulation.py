"""
تست شبیه‌سازی (فاز ۲) — خلوص، سناریوها و قابلیت‌های غیرقابل محاسبه.

دو لایهٔ تضمین:

1. **ایستا** — کد شبیه‌سازی نباید هیچ عملیات نوشتنی ORM داشته باشد.
2. **اجرایی** — خود سناریوها هم نباید در دیتابیس چیزی بنویسند
   (بررسی کامل در ``test_readonly_proof.py``).

همچنین قابلیت‌هایی که عمداً پیاده‌سازی نشده‌اند باید صریحاً
``unavailable`` باشند، نه اینکه عدد ساختگی برگردانند.
"""
import ast
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from craftflow_ai.simulation import (
    ORDER_RELEASE,
    PRODUCTION_SEQUENCE,
    SCENARIO_MATERIAL_AVAILABILITY,
    SCENARIO_ORDER_PRIORITY,
    STATION_CAPACITY,
    MaterialSnapshot,
    OpenIssueSnapshot,
    RawMaterialSnapshot,
    Scenario,
    Snapshot,
    material_snapshot,
    queue_snapshot,
    run,
    simulate_material_availability,
    simulate_order_priority,
)
from inventory.models import MaterialIssue, RawMaterial, StockMovement
from product.models import Order, ProductionTask

from .factories import make_leftover, make_order, make_raw_material, make_tasks

SIMULATION_DIR = Path('craftflow_ai') / 'simulation'

FORBIDDEN_CALLS = (
    '.save(', '.update(', '.create(', '.bulk_create(',
    '.bulk_update(', '.delete(', 'transaction.atomic',
)


def simulation_sources():
    return sorted(SIMULATION_DIR.glob('*.py'))


class SimulationPurityStaticTests(TestCase):
    """بررسی ایستای کد شبیه‌سازی."""

    def test_simulation_package_exists(self):
        files = {path.name for path in simulation_sources()}
        self.assertEqual(files, {'__init__.py', 'engine.py', 'models.py', 'scenarios.py'})

    def test_no_forbidden_write_operations_in_source(self):
        for path in simulation_sources():
            source = path.read_text(encoding='utf-8')
            for token in FORBIDDEN_CALLS:
                self.assertNotIn(token, source, f'{path.name} contains {token}')

    def test_source_parses_and_has_no_write_method_calls(self):
        """بررسی ساختاری مستقل از متن خام (در برابر فریب با کامنت/رشته)."""
        write_names = {'save', 'update', 'create', 'bulk_create',
                       'bulk_update', 'delete'}
        for path in simulation_sources():
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, 'attr', None)
                if name in write_names:
                    self.fail(f'{path.name}:{node.lineno} calls .{name}()')
                if name == 'atomic':
                    self.fail(f'{path.name}:{node.lineno} calls transaction.atomic')

    def test_engine_does_not_import_django_models(self):
        engine = (SIMULATION_DIR / 'engine.py').read_text(encoding='utf-8')
        self.assertNotIn('django.db', engine)
        self.assertNotIn('from product.models', engine)
        self.assertNotIn('from inventory.models', engine)

    def test_models_module_has_no_django_dependency(self):
        models_source = (SIMULATION_DIR / 'models.py').read_text(encoding='utf-8')
        self.assertNotIn('django', models_source)


class UnavailableCapabilityTests(TestCase):
    """قابلیت‌هایی که فاز ۲ نمی‌تواند بسازد، باید صریح unavailable باشند."""

    def test_station_capacity_is_unavailable(self):
        payload = STATION_CAPACITY.to_dict()
        self.assertEqual(payload['reason'], 'missing_station_capacity')
        self.assertIn('station_capacity_simulation', payload['blocked_capabilities'])

    def test_production_sequence_is_unavailable(self):
        payload = PRODUCTION_SEQUENCE.to_dict()
        self.assertEqual(payload['reason'], 'missing_dependency_graph')
        self.assertIn('production_sequence_optimization',
                      payload['blocked_capabilities'])

    def test_order_release_is_unavailable(self):
        payload = ORDER_RELEASE.to_dict()
        self.assertEqual(payload['reason'], 'missing_dependency_graph')
        self.assertIn('generated_task_count', payload['blocked_capabilities'])

    def test_no_capacity_tool_is_registered(self):
        from craftflow_ai.tools import get_registry
        names = set(get_registry().names())
        for forbidden in ('simulate_station_capacity', 'simulate_worker_availability',
                          'simulate_order_release', 'get_production_dependencies',
                          'get_order_impact'):
            self.assertNotIn(forbidden, names)

    def test_unavailable_capabilities_report_no_numbers(self):
        for capability in (STATION_CAPACITY, PRODUCTION_SEQUENCE, ORDER_RELEASE):
            payload = capability.to_dict()
            self.assertNotIn(0, [v for v in payload.values() if isinstance(v, int)])


class MaterialAvailabilityTests(TestCase):
    def setUp(self):
        self.order = make_order(status='producing')
        self.tasks = make_tasks(self.order, stations=('cut',), statuses=('pending',))
        self.raw = make_raw_material(name='رنگ سناریو', stock=Decimal('0'))
        MaterialIssue.objects.create(
            task=self.tasks[0], raw_material=self.raw,
            requested_quantity=Decimal('8'), issued_quantity=Decimal('0'),
            purpose='production', status='requested',
        )

    def test_additional_quantity_moves_projected_stock(self):
        result = simulate_material_availability(self.raw.pk, 20)
        state = result.projected_state
        self.assertEqual(state['current_stock'], '0.00')
        self.assertEqual(state['projected_stock'], '20.00')
        self.assertEqual(state['additional_quantity'], '20.00')

    def test_scenario_closes_a_real_shortage(self):
        shortage_before = simulate_material_availability(self.raw.pk, 0)
        shortage_after = simulate_material_availability(self.raw.pk, 100)
        self.assertFalse(shortage_before.projected_state['sufficient_before'])
        self.assertTrue(shortage_after.projected_state['sufficient_after'])

    def test_diff_reports_the_change(self):
        result = simulate_material_availability(self.raw.pk, 100)
        metrics = {row['metric']: row for row in result.diff.entries}
        self.assertIn('stock', metrics)
        self.assertIn('shortage', metrics)
        self.assertIn('sufficient', metrics)
        self.assertEqual(metrics['stock']['delta'], '100.00')

    def test_leftover_impact_is_reported(self):
        make_leftover(self.raw, Decimal('8'))
        result = simulate_material_availability(self.raw.pk, 0)
        state = result.projected_state
        self.assertEqual(state['baseline']['from_leftover'], '8.00')
        self.assertEqual(state['baseline']['physical_required'], '0.00')

    def test_no_open_requests_is_reported_as_no_effect(self):
        other = make_raw_material(name='رنگ بدون درخواست', stock=Decimal('3'))
        result = simulate_material_availability(other.pk, 10)
        self.assertEqual(result.no_effect_reason, 'no_open_requests_to_reallocate')
        self.assertEqual(result.affected_changes, [])

    def test_zero_quantity_is_explicitly_a_no_effect(self):
        result = simulate_material_availability(self.raw.pk, 0)
        self.assertEqual(result.no_effect_reason, 'additional_quantity <= 0')

    def test_result_declares_that_nothing_was_written(self):
        self.assertFalse(simulate_material_availability(self.raw.pk, 10)
                         .projected_state['write_performed'])

    def test_plan_logic_names_the_reused_service(self):
        result = simulate_material_availability(self.raw.pk, 10)
        self.assertIn('build_plans', result.projected_state['plan_logic'])

    def test_simulation_does_not_touch_the_database(self):
        before = {
            'issues': MaterialIssue.objects.count(),
            'movements': StockMovement.objects.count(),
            'stock': str(self.raw.current_stock),
        }
        simulate_material_availability(self.raw.pk, 500)
        self.raw.refresh_from_db()
        self.assertEqual(MaterialIssue.objects.count(), before['issues'])
        self.assertEqual(StockMovement.objects.count(), before['movements'])
        self.assertEqual(str(self.raw.current_stock), before['stock'])

    def test_snapshot_is_pure_data(self):
        snapshot = material_snapshot(self.raw.pk)
        self.assertIsInstance(snapshot, Snapshot)
        self.assertEqual(snapshot.kind, SCENARIO_MATERIAL_AVAILABILITY)
        material = snapshot.material(self.raw.pk)
        self.assertIsInstance(material, MaterialSnapshot)
        self.assertIsInstance(material.raw, RawMaterialSnapshot)
        for issue in material.issues:
            self.assertIsInstance(issue, OpenIssueSnapshot)

    def test_unknown_raw_material_is_insufficient_data(self):
        result = simulate_material_availability(999999, 10)
        self.assertEqual(result.status, 'insufficient_data')
        self.assertEqual(result.no_effect_reason, 'raw_material_not_in_snapshot')
        self.assertEqual(result.diff.entries, ())


class OrderPrioritySimulationTests(TestCase):
    def setUp(self):
        self.first = make_order(status='producing')
        self.second = make_order(status='producing')
        self.third = make_order(status='planned')
        make_tasks(self.first, stations=('cut',), statuses=('pending',))
        make_tasks(self.second, stations=('cut',), statuses=('pending',))
        make_tasks(self.third, stations=('cut',), statuses=('pending',))

    def test_priority_change_moves_the_order_to_the_front(self):
        target = Order.objects.order_by('-id').first()
        result = simulate_order_priority(target.id, 1)
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.projected_state['queue_position_after'], 1)
        self.assertGreater(result.projected_state['queue_position_before'],
                           result.projected_state['queue_position_after'])

    def test_effect_is_limited_to_queue_ordering(self):
        target = Order.objects.order_by('-id').first()
        state = simulate_order_priority(target.id, 1).projected_state
        self.assertEqual(state['effect'], 'queue ordering changes')
        self.assertIn('ProductionTask.step_order', state['not_affected'])
        self.assertIn('finish_time', state['not_affected'])

    def test_no_completion_time_is_promised(self):
        target = Order.objects.order_by('-id').first()
        result = simulate_order_priority(target.id, 1)
        serialized = str(result.to_dict())
        for forbidden in ('hours_earlier', 'sooner', 'finish_date', 'eta'):
            self.assertNotIn(forbidden, serialized)

    def test_scenario_matching_current_priority_reports_no_contrast(self):
        """همهٔ سفارش‌های زنده priority=3 دارند؛ سناریوی هم‌ارزش باید صریح بی‌اثر باشد."""
        target = Order.objects.order_by('-id').first()
        result = simulate_order_priority(target.id, target.priority)
        self.assertEqual(result.status, 'no_change')
        self.assertEqual(result.no_effect_reason,
                         'scenario_priority_equals_current_priority')
        self.assertFalse(result.projected_state['queue_order_changed'])
        self.assertEqual(result.diff.entries, ())

    def test_constant_priority_in_live_data_is_detectable(self):
        """اگر همهٔ اولویت‌ها یکسان باشند، جابه‌جایی فقط از سفارش هدف می‌آید."""
        priorities = set(Order.objects.values_list('priority', flat=True))
        target = Order.objects.order_by('-id').first()
        result = simulate_order_priority(target.id, 1)
        if len(priorities) == 1:
            self.assertEqual(result.projected_state['moved_orders'],
                             len(Order.objects.exclude(status='completed')))

    def test_step_order_is_never_modified(self):
        target = Order.objects.order_by('-id').first()
        before = list(
            ProductionTask.objects.filter(order_id=target.id)
            .values_list('id', 'step_order')
        )
        simulate_order_priority(target.id, 1)
        after = list(
            ProductionTask.objects.filter(order_id=target.id)
            .values_list('id', 'step_order')
        )
        self.assertEqual(before, after)

    def test_priority_is_never_written(self):
        target = Order.objects.order_by('-id').first()
        before = target.priority
        simulate_order_priority(target.id, 1)
        target.refresh_from_db()
        self.assertEqual(target.priority, before)

    def test_completed_order_is_not_in_the_queue(self):
        done = make_order(status='completed')
        result = simulate_order_priority(done.id, 1)
        self.assertEqual(result.status, 'insufficient_data')
        self.assertEqual(result.no_effect_reason, 'order_not_in_open_queue')

    def test_missing_scenario_parameters_are_reported(self):
        result = simulate_order_priority(1, None)
        self.assertEqual(result.no_effect_reason, 'missing_scenario_parameters')
        result = run(queue_snapshot(), Scenario(kind=SCENARIO_ORDER_PRIORITY,
                                                params={'order_id': 1}))
        self.assertEqual(result.status, 'insufficient_data')

    def test_change_list_is_bounded(self):
        for index in range(30):
            order = make_order(status='planned')
            make_tasks(order, stations=('cut',), statuses=('pending',))
        target = Order.objects.order_by('-id').first()
        result = simulate_order_priority(target.id, 1)
        self.assertLessEqual(len(result.diff.entries), 50)
        if result.projected_state['moved_orders_truncated']:
            self.assertEqual(
                result.projected_state['moved_orders_reported'],
                result.projected_state['moved_orders_truncated'] * 50,
            )


class EngineBoundaryTests(TestCase):
    def test_unsupported_scenario_is_insufficient_data(self):
        result = run(Snapshot(kind='x'), Scenario(kind='not_a_scenario'))
        self.assertEqual(result.status, 'insufficient_data')
        self.assertEqual(result.no_effect_reason, 'unsupported_scenario')

    def test_engine_rejects_wrong_types(self):
        with self.assertRaises(TypeError):
            run({'kind': 'x'}, Scenario(kind=SCENARIO_ORDER_PRIORITY))
        with self.assertRaises(TypeError):
            run(Snapshot(kind='x'), {'kind': SCENARIO_ORDER_PRIORITY})

    def test_engine_works_on_a_hand_written_snapshot_without_orm(self):
        """موتور باید بتواند بدون دیتابیس هم کار کند — این خالص بودن را ثابت می‌کند."""
        snapshot = Snapshot(
            kind=SCENARIO_MATERIAL_AVAILABILITY,
            materials=(MaterialSnapshot(
                raw=RawMaterialSnapshot(
                    pk=1, name='ماده', unit='kg', pack_size=Decimal('5'),
                    current_stock=Decimal('0'), min_stock_alert=Decimal('0'),
                ),
                leftover=Decimal('0'),
                issues=(OpenIssueSnapshot(pk=1, remaining_quantity=Decimal('8')),),
            ),),
        )
        result = run(snapshot, Scenario(
            kind=SCENARIO_MATERIAL_AVAILABILITY,
            params={'raw_material_id': 1, 'additional_quantity': '100'},
        ))
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.projected_state['projected_stock'], '100.00')
        self.assertTrue(result.projected_state['sufficient_after'])

    def test_engine_does_not_mutate_the_snapshot(self):
        raw = make_raw_material(name='ماده خالص', stock=Decimal('1'))
        snapshot = material_snapshot(raw.pk)
        before = snapshot.material(raw.pk).raw.current_stock
        run(snapshot, Scenario(
            kind=SCENARIO_MATERIAL_AVAILABILITY,
            params={'raw_material_id': raw.pk, 'additional_quantity': '50'},
        ))
        self.assertEqual(snapshot.material(raw.pk).raw.current_stock, before)

    def test_every_simulation_result_carries_confidence_and_limitations(self):
        raw = make_raw_material(name='ماده محدودیت', stock=Decimal('1'))
        result = simulate_material_availability(raw.pk, 5)
        self.assertIn(result.confidence, ('high', 'medium', 'low'))
        self.assertTrue(result.limitations)
        self.assertTrue(result.evidence)


class RawMaterialSnapshotTests(TestCase):
    def test_with_stock_returns_a_copy(self):
        raw = make_raw_material(name='کپی', stock=Decimal('5'))
        snapshot = material_snapshot(raw.pk).material(raw.pk).raw
        moved = snapshot.with_stock(5)
        self.assertEqual(snapshot.current_stock, Decimal('5.00'))
        self.assertEqual(moved.current_stock, Decimal('10.00'))

    def test_money_helper_is_stringified(self):
        from craftflow_ai.simulation.models import money
        self.assertEqual(money(Decimal('1.005')), '1.00')
        self.assertIsNone(money(None))