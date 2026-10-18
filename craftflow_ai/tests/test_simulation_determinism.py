"""
قطعی‌بودن نتایج فاز ۲ (Determinism Tests).

دو چیز قفل می‌شود:

1. **اجرای دوبارهٔ یک سناریو/تحلیل روی دیتابیس بدون تغییر** باید خروجی
   کاملاً یکسان بدهد (برابری عمقی).
2. **ترتیب فراخوانی ابزار توسط مدل** نباید payload هیچ ابزاری را تغییر دهد.

مبدأ: گزارش‌های تحلیل و شبیه‌سازی قرار است در حافظهٔ کاربر و در گفتگو
دوباره خوانده شوند؛ اگر دو اجرای یکسان روی یک دیتابیس یکسان دو متن متفاوت
دهند، هم اعتماد کاربر می‌ریزد و هم تست‌های پذیرش ناپایدار می‌شوند.
"""
import copy
import json
from decimal import Decimal

from django.test import TestCase

from craftflow_ai.analysis.inventory import material_impact
from craftflow_ai.analysis.order import order_delay, order_health
from craftflow_ai.analysis.production import station_bottleneck, unknown_station_bucket
from craftflow_ai.orchestrator.manager import AIOrchestrator
from craftflow_ai.providers.local import FakeAIProvider
from craftflow_ai.simulation import (
    simulate_material_availability,
    simulate_order_priority,
)
from inventory.models import (
    DailyMaterialQueue,
    StockMovement,
    RawMaterial,
)
from product.models import Order, ProductionTask

from .factories import (
    make_open_issue,
    make_order,
    make_raw_material,
    make_tasks,
    make_user,
)

# فیلدهایی که ذاتاً «اکنونِ» دیتابیس را نشان می‌دهند و برای مقایسهٔ قطعیت
# کنار گذاشته می‌شوند؛ بقیهٔ خروجی باید کاملاً یکسان باشد.
VOLATILE_KEYS = frozenset({
    'generated_at',
    'captured_at',
    'reference_time',
    'today',
    'jalali_today',
    'now',
})


def strip_volatile(payload):
    """حذف بازگشتی فیلدهای زمان‌محور تا مقایسهٔ عمقی معنادار شود."""
    if isinstance(payload, dict):
        return {
            key: strip_volatile(value)
            for key, value in payload.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(payload, (list, tuple)):
        return [strip_volatile(item) for item in payload]
    return payload


class SimulationDeterminismTests(TestCase):
    def setUp(self):
        self.order = make_order(status='producing', customer_name='قطعیت')
        self.tasks = make_tasks(self.order, stations=('cut', 'paint'),
                                statuses=('pending', 'waiting'))
        self.raw = make_raw_material('رنگ قطعیت', stock=Decimal('0'),
                                     min_alert=Decimal('5'))
        make_open_issue(self.raw, self.order, quantity=Decimal('8'))

    def test_material_scenario_is_deeply_equal_twice(self):
        first = strip_volatile(simulate_material_availability(self.raw.pk, 20).to_dict())
        second = strip_volatile(simulate_material_availability(self.raw.pk, 20).to_dict())
        self.assertEqual(first, second)

    def test_priority_scenario_is_deeply_equal_twice(self):
        first = strip_volatile(simulate_order_priority(self.order.id, 1).to_dict())
        second = strip_volatile(simulate_order_priority(self.order.id, 1).to_dict())
        self.assertEqual(first, second)

    def test_repeated_scenario_does_not_accumulate_changes(self):
        baseline = strip_volatile(simulate_order_priority(self.order.id, 1).to_dict())
        for _ in range(3):
            again = strip_volatile(simulate_order_priority(self.order.id, 1).to_dict())
            self.assertEqual(baseline, again)

    def test_scenario_does_not_change_the_queue(self):
        before = list(
            Order.objects.order_by('priority', 'id').values_list('id', 'priority')
        )
        simulate_order_priority(self.order.id, 1)
        after = list(
            Order.objects.order_by('priority', 'id').values_list('id', 'priority')
        )
        self.assertEqual(before, after)

    def test_scenario_writes_nothing_to_the_database(self):
        baseline = {
            'issues': DailyMaterialQueue.objects.count(),
            'leftovers': StockMovement.objects.filter(movement_type='return').count(),
            'movements': StockMovement.objects.count(),
            'orders': Order.objects.count(),
            'tasks': ProductionTask.objects.count(),
        }
        stock = str(self.raw.current_stock)

        simulate_material_availability(self.raw.pk, 1000)
        simulate_order_priority(self.order.id, 1)

        after = {
            'issues': DailyMaterialQueue.objects.count(),
            'leftovers': StockMovement.objects.filter(movement_type='return').count(),
            'movements': StockMovement.objects.count(),
            'orders': Order.objects.count(),
            'tasks': ProductionTask.objects.count(),
        }
        self.assertEqual(baseline, after)
        self.raw.refresh_from_db()
        self.assertEqual(str(self.raw.current_stock), stock)


class AnalysisDeterminismTests(TestCase):
    def setUp(self):
        self.order = make_order(status='producing', customer_name='قطعیت تحلیل')
        make_tasks(self.order, stations=('cut', 'paint'),
                   statuses=('pending', 'waiting'))

    def test_order_health_is_deeply_equal_twice(self):
        self.assertEqual(strip_volatile(order_health(self.order.id)),
                         strip_volatile(order_health(self.order.id)))

    def test_order_delay_is_deeply_equal_twice(self):
        self.assertEqual(strip_volatile(order_delay(self.order.id)),
                         strip_volatile(order_delay(self.order.id)))

    def test_station_bottleneck_is_deeply_equal_twice(self):
        self.assertEqual(strip_volatile(station_bottleneck('paint')),
                         strip_volatile(station_bottleneck('paint')))

    def test_material_impact_is_deeply_equal_twice(self):
        raw = make_raw_material('رنگ تحلیل قطعیت')
        self.assertEqual(strip_volatile(material_impact(raw_material_id=raw.pk)),
                         strip_volatile(material_impact(raw_material_id=raw.pk)))

    def test_unknown_station_bucket_is_deeply_equal_twice(self):
        self.assertEqual(strip_volatile(unknown_station_bucket()),
                         strip_volatile(unknown_station_bucket()))

    def test_analysis_is_side_effect_free_for_the_returned_object(self):
        report = order_health(self.order.id)
        before = copy.deepcopy(report)
        order_health(self.order.id)
        order_delay(self.order.id)
        self.assertEqual(report, before)


class ToolCallOrderDoesNotAffectResultsTests(TestCase):
    """
    ترتیب فراخوانی ابزار توسط مدل نباید روی نتیجهٔ هیچ ابزاری اثر بگذارد.

    این تست همان سناریوی واقعی را بازسازی می‌کند: یک نوبت گفتگو که مدل در آن
    دو ابزار را صدا می‌زند و ترتیبشان بین دو اجرا جابه‌جا شده است.
    """

    def setUp(self):
        self.user = make_user('determinism_user', groups=['1'])
        self.order = make_order(status='producing', customer_name='قطعیت ابزار')
        make_tasks(self.order, stations=('cut', 'paint'),
                   statuses=('pending', 'waiting'))
        self.raw = make_raw_material('رنگ ابزار قطعیت', stock=Decimal('0'),
                                     min_alert=Decimal('5'))
        make_open_issue(self.raw, self.order, quantity=Decimal('8'))

    def _run(self, tool_specs, message='بررسی کن'):
        """
        یک نوبت کامل گفتگو را اجرا می‌کند و شواهد واقعیِ رسیده به مدل را
        به‌ تفکیک نام ابزار برمی‌گرداند (نه فقط نام و آرگومان‌ها).
        """
        provider = FakeAIProvider()
        for name, arguments in tool_specs:
            provider.queue_tool_call(name, arguments)
        provider.queue_text('خلاصهٔ نهایی بر پایهٔ شواهد ابزارها.')
        result = AIOrchestrator(self.user, provider=provider).chat(message)
        self.assertTrue(result.success, getattr(result, 'error_code', ''))
        self.assertEqual([c['status'] for c in result.tool_calls],
                         ['completed'] * len(tool_specs),
                         result.tool_calls)

        # شواهدی که واقعاً به مدل داده شده‌اند. هر فراخوانی ابزار یک نوبت provider
        # می‌سازد و تاریخچه در نوبت آخر کامل بازپخش می‌شود؛ پس همهٔ پیام‌های
        # tool در آخرین نوبت موجودند و ترتیبشان ترتیب اجرای ابزارها است.
        names = [call['name'] for call in result.tool_calls]
        tool_messages = [
            item for item in provider.calls[-1]['messages']
            if item.get('role') == 'tool'
        ]
        self.assertEqual(len(tool_messages), len(names))
        return {
            name: strip_volatile(json.loads(message['content']))
            for name, message in zip(names, tool_messages)
        }

    def _expected(self, name, arguments):
        from craftflow_ai.tools import get_registry
        tool = get_registry().get(name)
        return strip_volatile(tool.run(**arguments)['data'])

    def test_two_tools_in_either_order_return_identical_evidence(self):
        specs = [
            ('analyze_order_health', {'order_id': self.order.id}),
            ('analyze_station_bottleneck', {'stage': 'paint'}),
        ]
        forward = self._run(specs)
        backward = self._run(list(reversed(specs)))

        self.assertEqual(sorted(forward), sorted(backward))
        self.assertEqual(forward, backward)

        for name, arguments in specs:
            with self.subTest(tool=name):
                self.assertEqual(forward[name]['data'],
                                 self._expected(name, arguments))

    def test_evidence_matches_direct_tool_invocation_in_both_orders(self):
        specs = [
            ('analyze_material_impact', {'raw_material_id': self.raw.pk}),
            ('simulate_material_availability',
             {'raw_material_id': self.raw.pk, 'additional_quantity': 5}),
        ]
        forward = self._run(specs)
        backward = self._run(list(reversed(specs)))
        self.assertEqual(forward, backward)
        for name, arguments in specs:
            with self.subTest(tool=name):
                self.assertEqual(backward[name]['data'],
                                 self._expected(name, arguments))

    def test_identical_turns_produce_identical_evidence(self):
        specs = [
            ('analyze_order_health', {'order_id': self.order.id}),
            ('analyze_order_delay', {'order_id': self.order.id}),
        ]
        self.assertEqual(self._run(specs), self._run(specs))

    def test_repeated_turns_do_not_drift_after_an_intervening_turn(self):
        specs = [('analyze_order_health', {'order_id': self.order.id})]
        first = self._run(specs)
        self._run([('analyze_station_bottleneck', {'stage': 'cut'})])
        again = self._run(specs)
        self.assertEqual(first, again)

    def test_simulation_tools_are_also_order_independent(self):
        specs = [
            ('simulate_order_priority', {'order_id': self.order.id,
                                         'new_priority': 1}),
            ('simulate_material_availability',
             {'raw_material_id': self.raw.pk, 'additional_quantity': 3}),
        ]
        forward = self._run(specs)
        backward = self._run(list(reversed(specs)))
        self.assertEqual(forward, backward)
        self.assertEqual(RawMaterial.objects.count(), 1)
        self.assertEqual(DailyMaterialQueue.objects.count(), 1)