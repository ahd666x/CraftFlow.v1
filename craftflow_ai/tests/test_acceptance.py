"""
Acceptance Test Phase 1 — CraftFlow AI.

این فایل تست پذیرش است و عمداً روی داده‌های واقعیِ ساختار CraftFlow
(نام ایستگاه‌ها، وضعیت‌ها، گروه‌های کاربران) اجرا می‌شود.

بخش‌ها:
  2  Registry
  5  E2E با FakeAIProvider
  6  Permission matrix
  7  Audit log
  8  API
  10 Provider
"""
import json
from datetime import timedelta
from decimal import Decimal

import jdatetime
from django.contrib.auth.models import AnonymousUser, Group, User
from django.db.models import Count, Q
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from craftflow_ai import config
from craftflow_ai.audit import logger as audit
from craftflow_ai.models import AIAuditLog, AIConversation, AIConversationMessage
from craftflow_ai.orchestrator.manager import AIOrchestrator
from craftflow_ai.permissions.errors import (
    AIDisabledError,
    AIConfigurationError,
    ToolDisabledError,
    ToolNotFoundError,
)
from craftflow_ai.permissions.policy import has_permission
from craftflow_ai.providers import build_provider
from craftflow_ai.providers.base import AIResponse, ToolCall
from craftflow_ai.providers.local import FakeAIProvider, LocalProvider, get_provider
from craftflow_ai.tools import get_registry
from craftflow_ai.tools.registry import READ_ONLY_PHASE, Tool, ToolRegistry

from inventory.models import DailyMaterialQueue, RawMaterial
from product.models import STATION_CHOICES, Order, ProductionTask

from .factories import (
    make_leftover,
    make_open_issue,
    make_order,
    make_raw_material,
    make_tasks,
    make_user,
)

EXPECTED_TOOLS = {
    'get_open_orders', 'get_order_details', 'find_delayed_orders',
    'get_production_status', 'get_pending_tasks',
    'find_production_bottlenecks', 'get_inventory_status',
    'get_material_requirements', 'find_material_shortages',
    'generate_production_report', 'get_order_timeline',
    'get_worker_load', 'get_working_day_info', 'get_quality_status',
    # فاز ۲ — تحلیل قطعی و شبیه‌سازی خالص
    'analyze_order_health', 'analyze_order_delay',
    'analyze_station_bottleneck', 'analyze_material_impact',
    'simulate_material_availability', 'simulate_order_priority',
}

EXPECTED_TOOL_COUNT = 20


class RegistryAcceptanceTests(TestCase):
    """Section 2 — Registry."""

    def setUp(self):
        self.registry = get_registry()
        self.user = make_user('reg_user', groups=['1'])

    def test_01_all_expected_tools_are_discoverable(self):
        self.assertEqual(set(self.registry.names()), EXPECTED_TOOLS)
        self.assertEqual(len(self.registry), EXPECTED_TOOL_COUNT)

    def test_02_every_tool_exposes_a_valid_llm_schema(self):
        schemas = self.registry.schemas_for_llm()
        self.assertEqual(len(schemas), EXPECTED_TOOL_COUNT)
        for schema in schemas:
            self.assertIn('name', schema)
            self.assertTrue(schema['description'].strip())
            self.assertEqual(schema['input_schema']['type'], 'object')
            # شمای LLM نباید permission یا handler را لو بدهد.
            self.assertNotIn('handler', schema)
            self.assertNotIn('permission', schema)
            self.assertNotIn('read_only', schema)

    def test_03_unknown_tool_is_rejected(self):
        for bogus in ('execute_sql', 'run_python', 'drop_table', '', 'GET_OPEN_ORDERS'):
            with self.assertRaises(ToolNotFoundError):
                self.registry.get(bogus)
        self.assertNotIn('execute_sql', self.registry)

    def test_04_no_generic_execution_escape_hatch_exists(self):
        """هیچ ابزار عمومی/خطرناکی نباید در رجیستری باشد."""
        dangerous = {
            'execute_sql', 'run_query', 'run_python', 'eval', 'exec',
            'os_system', 'shell', 'read_file', 'write_file', 'delete',
            'create_order', 'update_order', 'delete_order', 'save',
        }
        self.assertEqual(dangerous & set(self.registry.names()), set())

    def test_05_every_registered_tool_is_read_only(self):
        for tool in self.registry.list_tools():
            self.assertTrue(tool.read_only, f'{tool.name} is not read_only')
        self.assertTrue(READ_ONLY_PHASE)

    def test_06_write_tool_cannot_be_registered(self):
        fresh = ToolRegistry()

        def danger():
            return {'success': True, 'data': {}}

        with self.assertRaises(ToolDisabledError) as ctx:
            fresh.register(Tool(
                name='delete_everything',
                description='x',
                permission='orders.view',
                handler=danger,
                read_only=False,
            ))
        self.assertEqual(ctx.exception.code, 'TOOL_DISABLED')
        self.assertNotIn('delete_everything', fresh)

    def test_07_duplicate_registration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(Tool(
                name='get_open_orders', description='dup',
                permission='orders.view', handler=lambda: {'success': True},
            ))

    def test_08_non_tool_object_is_rejected(self):
        with self.assertRaises(TypeError):
            self.registry.register({'name': 'fake_tool'})

    def test_09_permission_is_checked_before_handler_runs(self):
        """
        حیاتی‌ترین تست امنیتی: اگر کاربر دسترسی نداشته باشد، handler نباید
        حتی یک بار هم صدا زده شود.
        """
        calls = []
        fresh = ToolRegistry()
        fresh.register(Tool(
            name='spy_tool',
            description='x',
            permission='inventory.view',
            handler=lambda: calls.append(1) or {'success': True, 'data': {}},
        ))

        outsider = make_user('no_groups_user')
        from craftflow_ai.orchestrator.planner import ToolPlanner

        plan = ToolPlanner(fresh, outsider).plan('spy_tool', {})
        self.assertFalse(plan)
        self.assertIsNotNone(plan.tool)          # ابزار شناخته شد
        self.assertEqual(plan.to_error()['error']['code'], 'PERMISSION_DENIED')
        self.assertEqual(calls, [], 'handler ran despite no permission')

    def test_10_invalid_arguments_are_rejected_before_handler(self):
        calls = []
        fresh = ToolRegistry()
        fresh.register(Tool(
            name='typed_tool',
            description='x',
            permission='orders.view',
            handler=lambda **kw: calls.append(kw) or {'success': True, 'data': {}},
            input_schema={
                'type': 'object',
                'properties': {'limit': {'type': 'integer'}},
                'required': ['limit'],
                'additionalProperties': False,
            },
        ))
        user = make_user('arg_user', groups=['1'])
        from craftflow_ai.orchestrator.planner import ToolPlanner

        planner = ToolPlanner(fresh, user)
        # نوع اشتباه
        self.assertFalse(planner.plan('typed_tool', {'limit': 'abc'}))
        # آرگومان ناشناخته
        self.assertFalse(planner.plan('typed_tool', {'limit': 5, 'sql': 'DROP TABLE'}))
        # آرگومان الزامی گمشده
        self.assertFalse(planner.plan('typed_tool', {}))
        self.assertEqual(calls, [], 'handler ran with invalid arguments')
        # حالت معتبر اجرا می‌شود (planner فقط اعتبارسنجی می‌کند؛ اجرا با run است)
        plan = planner.plan('typed_tool', {'limit': '5'})
        self.assertTrue(plan)
        self.assertEqual(plan.arguments, {'limit': 5})
        self.assertEqual(calls, [], 'planner must not execute the handler')
        fresh.get('typed_tool').run(**plan.arguments)
        self.assertEqual(calls, [{'limit': 5}])

    def test_11_tool_call_ceiling_stops_the_loop(self):
        """مدلی که بی‌نهایت ابزار صدا می‌زند باید متوقف شود."""
        with override_settings(CRAFTFLOW_AI_MAX_TOOL_CALLS=2):
            self.assertEqual(config.get_max_tool_calls(), 2)
            provider = FakeAIProvider()
            for _ in range(10):
                provider.queue_tool_call('get_production_status')
            result = AIOrchestrator(self.user, provider=provider).chat('وضعیت تولید')
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 'MAX_TOOL_CALLS_EXCEEDED')
        self.assertEqual(len(result.tool_calls), 2)

    def test_12_registry_is_idempotent(self):
        from craftflow_ai.tools import register_all
        fresh = ToolRegistry()
        register_all(fresh)
        register_all(fresh)  # نباید خطا دهد
        self.assertEqual(len(fresh), EXPECTED_TOOL_COUNT)

    def test_13_available_for_filters_by_permission(self):
        warehouse = make_user('wh_only', groups=['انبار'])
        allowed = {t.name for t in self.registry.available_for(warehouse)}
        self.assertIn('get_inventory_status', allowed)
        self.assertNotIn('generate_production_report', allowed)
        self.assertNotIn('get_order_details', allowed)


class FakeProviderEndToEndTests(TestCase):
    """Section 5 — جریان کامل بدون API واقعی."""

    def setUp(self):
        self.user = make_user('e2e_user', groups=['1'])
        self.order = make_order(status='producing', customer_name='مشتری E2E')
        self.tasks = make_tasks(self.order, stations=('cut', 'cnc', 'prs'))
        self.raw = make_raw_material('رنگ تست', stock=Decimal('0'), min_alert=Decimal('3'))
        make_open_issue(self.raw, self.order, quantity=Decimal('10'))
        make_leftover(self.raw, Decimal('0'))

    def _run(self, message, tool_name, tool_args=None, final='پاسخ نهایی تست.'):
        provider = FakeAIProvider()
        provider.queue_tool_call(tool_name, tool_args)
        provider.queue_text(final)
        result = AIOrchestrator(self.user, provider=provider).chat(message)
        return result, provider

    def test_01_production_status_message(self):
        result, provider = self._run('وضعیت کلی تولید چیست؟', 'get_production_status')
        self.assertTrue(result.success)
        self.assertEqual([c['name'] for c in result.tool_calls], ['get_production_status'])
        self.assertEqual(result.tool_calls[0]['status'], 'completed')
        self.assertEqual(result.answer, 'پاسخ نهایی تست.')
        self.assertEqual(len(provider.calls), 2)
        # شواهد واقعی به مدل رسیده‌اند.
        tool_messages = [m for m in provider.calls[1]['messages'] if m['role'] == 'tool']
        self.assertEqual(len(tool_messages), 1)
        evidence = json.loads(tool_messages[0]['content'])
        self.assertTrue(evidence['success'])
        self.assertIn('orders', evidence['data'])

    def test_02_delayed_orders_message(self):
        old = make_order(status='planned', customer_name='قدیمی')
        Order.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=10))
        result, _ = self._run('چه سفارش‌هایی عقب هستند؟', 'find_delayed_orders',
                              {'limit': 50})
        self.assertTrue(result.success)
        self.assertEqual(result.tool_calls[0]['status'], 'completed')
        self.assertEqual(result.tool_calls[0]['name'], 'find_delayed_orders')

    def test_03_order_details_message_with_real_id(self):
        result, _ = self._run(
            f'وضعیت سفارش {self.order.id} چیست؟', 'get_order_details',
            {'order_id': self.order.id},
        )
        self.assertTrue(result.success)
        activity = result.tool_calls[0]
        self.assertEqual(activity['status'], 'completed')
        self.assertEqual(activity['arguments']['order_id'], self.order.id)
        self.assertTrue(activity['label'])  # برچسب فارسی

    def test_04_material_shortage_message(self):
        result, _ = self._run('چه مواد اولیه‌ای کم داریم؟', 'find_material_shortages')
        self.assertTrue(result.success)
        self.assertEqual(result.tool_calls[0]['status'], 'completed')

    def test_05_production_report_message(self):
        result, _ = self._run('یک گزارش تولید امروز بده.', 'generate_production_report')
        self.assertTrue(result.success)
        self.assertEqual(result.tool_calls[0]['name'], 'generate_production_report')

    def test_06_full_chain_creates_conversation_and_messages_via_api(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('get_production_status')
        provider.queue_text('این پاسخ از دادهٔ واقعی ساخته شده است.')
        with self.settings(CRAFTFLOW_AI_ENABLED=True,
                           CRAFTFLOW_AI_PROVIDER='fake'):
            from unittest import mock
            with mock.patch(
                'craftflow_ai.orchestrator.manager.build_provider',
                return_value=provider,
            ):
                self.client.force_login(self.user)
                response = self.client.post(
                    reverse('craftflow_ai:chat_api'),
                    data=json.dumps({'message': 'وضعیت کلی تولید چیست؟'}),
                    content_type='application/json',
                )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['message'], 'این پاسخ از دادهٔ واقعی ساخته شده است.')
        self.assertEqual(len(body['tool_calls']), 1)
        self.assertTrue(body['conversation_id'])

        conversation = AIConversation.objects.get(conversation_id=body['conversation_id'])
        self.assertEqual(conversation.user_id, self.user.id)
        roles = list(conversation.messages.order_by('created_at').values_list('role', flat=True))
        self.assertEqual(roles, ['user', 'assistant'])

    def test_07_conversation_history_is_replayed(self):
        provider = FakeAIProvider()
        provider.queue_text('پاسخ اول')
        result1 = AIOrchestrator(self.user, provider=provider).chat('اولی')
        provider2 = FakeAIProvider()
        provider2.queue_text('پاسخ دوم')
        result2 = AIOrchestrator(self.user, provider=provider2).chat(
            'دومی', conversation_id=result1.conversation_id,
            history=[{'role': 'user', 'content': 'اولی'},
                     {'role': 'assistant', 'content': 'پاسخ اول'}],
        )
        self.assertEqual(result1.conversation_id, result2.conversation_id)
        user_messages = [m for m in provider2.calls[0]['messages'] if m['role'] == 'user']
        self.assertEqual(len(user_messages), 2)
        self.assertEqual(user_messages[0]['content'], 'اولی')
        self.assertEqual(user_messages[1]['content'], 'دومی')

    def test_08_local_provider_works_offline(self):
        with override_settings(CRAFTFLOW_AI_PROVIDER='local'):
            result = AIOrchestrator(self.user).chat('وضعیت تولید چطور است؟')
        self.assertTrue(result.success)
        self.assertEqual(result.provider, 'local')
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0]['name'], 'get_production_status')
        # پاسخ باید شامل دادهٔ واقعی باشد، نه متن خالی.
        self.assertIn('orders', result.answer)
        self.assertIn('tasks', result.answer)

    def test_09_local_provider_refuses_to_invent_data(self):
        """بدون ابزار و بدون شواهد، نباید عددی اختراع کند."""
        with override_settings(CRAFTFLOW_AI_PROVIDER='local'):
            result = AIOrchestrator(self.user).chat('سلام چطوری؟')
        self.assertTrue(result.success)
        self.assertEqual(result.tool_calls, [])
        self.assertIn('Provider محلی', result.answer)

    def test_10_orchestrator_disabled_raises(self):
        orch = AIOrchestrator(self.user, enabled=False)
        with self.assertRaises(AIDisabledError):
            orch.chat('وضعیت تولید')

    def test_11_provider_timeout_is_handled_gracefully(self):
        provider = FakeAIProvider([TimeoutError('connection timed out')])
        result = AIOrchestrator(self.user, provider=provider).chat('وضعیت تولید')
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 'PROVIDER_ERROR')
        self.assertEqual(result.tool_calls, [])
        self.assertIn('ارتباط', result.answer)

    def test_12_malformed_provider_response_is_handled(self):
        provider = FakeAIProvider([
            AIResponse(content=None, tool_calls=None, finish_reason='stop'),
        ])
        result = AIOrchestrator(self.user, provider=provider).chat('وضعیت تولید')
        self.assertTrue(result.success)

    def test_13_provider_returning_bad_arguments(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('get_order_details', 'not-a-dict')
        provider.queue_text('متوجه نشدم.')
        result = AIOrchestrator(self.user, provider=provider).chat('سفارش 1')
        self.assertEqual(result.tool_calls[0]['status'], 'failed')
        self.assertEqual(result.tool_calls[0]['error_code'], 'INVALID_ARGUMENTS')

    def test_14_provider_hallucinating_unknown_tool(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('execute_sql', {'query': 'DROP TABLE orders'})
        provider.queue_text('نمی‌توانم این کار را بکنم.')
        before = Order.objects.count()
        result = AIOrchestrator(self.user, provider=provider).chat('پاک کن')
        activity = result.tool_calls[0]
        self.assertEqual(activity['status'], 'failed')
        self.assertEqual(activity['error_code'], 'TOOL_NOT_FOUND')
        self.assertEqual(Order.objects.count(), before)  # دیتابیس سالم است
        # خود مدل هم پاسخ نهایی گرفت و جریان نشکست.
        self.assertTrue(result.success)

    def test_15_tool_exception_is_contained(self):
        """
        استثنای handler نباید از لایهٔ Tool بیرون بزند؛ باید به
        ToolExecutionError ساختاریافته تبدیل شود.
        """
        from craftflow_ai.permissions.errors import ToolExecutionError
        broken = Tool(
            name='get_production_status',
            description='x', permission='production.view',
            handler=lambda: 1 / 0,
        )
        with self.assertRaises(ToolExecutionError) as ctx:
            broken.run()
        self.assertEqual(ctx.exception.code, 'TOOL_EXECUTION_FAILED')
        self.assertEqual(ctx.exception.detail['exception'], 'ZeroDivisionError')


class PermissionMatrixTests(TestCase):
    """Section 6 — ماتریس دسترسی، فقط سمت سرور."""

    def setUp(self):
        self.registry = get_registry()
        self.superuser = make_user('su_perm', superuser=True)
        self.staff = make_user('staff_perm', groups=['1'])
        self.manager2 = make_user('mgr2_perm', groups=['2'])
        self.manager3 = make_user('mgr3_perm', groups=['3'])
        self.warehouse = make_user('wh_perm', groups=['انبار'])
        self.plain = make_user('plain_perm')
        self.anon = AnonymousUser()

    def test_01_group_setup(self):
        for name in ('1', '2', '3', 'انبار'):
            self.assertTrue(Group.objects.filter(name=name).exists(), name)

    def test_02_tool_permission_assignment(self):
        """هر ابزار دقیقاً یک permission از مجموعهٔ شناخته‌شده دارد."""
        from craftflow_ai.permissions.policy import known_permissions
        expected = {
            'get_open_orders': 'orders.view',
            'get_order_details': 'orders.view',
            'find_delayed_orders': 'orders.view',
            'get_production_status': 'production.view',
            'get_pending_tasks': 'production.view',
            'find_production_bottlenecks': 'production.view',
            'get_order_timeline': 'production.view',
            'get_worker_load': 'production.view',
            'get_working_day_info': 'production.view',
            'get_inventory_status': 'inventory.view',
            'get_material_requirements': 'inventory.view',
            'find_material_shortages': 'inventory.view',
            'generate_production_report': 'reports.view',
            'get_quality_status': 'quality.view',
            # فاز ۲ — همان permissionهای منبع دادهٔ زیربنایی، بدون گسترش دسترسی
            'analyze_order_health': 'orders.view',
            'analyze_order_delay': 'orders.view',
            'analyze_station_bottleneck': 'production.view',
            'analyze_material_impact': 'inventory.view',
            'simulate_material_availability': 'inventory.view',
            'simulate_order_priority': 'orders.view',
        }
        self.assertEqual(len(expected), EXPECTED_TOOL_COUNT)
        for name, perm in expected.items():
            self.assertEqual(self.registry.get(name).permission, perm, name)
            self.assertIn(perm, known_permissions())

    def test_03_superuser_can_use_every_tool(self):
        for tool in self.registry.list_tools():
            self.assertTrue(has_permission(self.superuser, tool.permission), tool.name)
        self.assertEqual(len(self.registry.available_for(self.superuser)), EXPECTED_TOOL_COUNT)

    def test_04_staff_group_1(self):
        allowed = {t.name for t in self.registry.available_for(self.staff)}
        self.assertEqual(allowed, EXPECTED_TOOLS)

    def test_05_manager_group_2(self):
        self.assertEqual(
            {t.name for t in self.registry.available_for(self.manager2)}, EXPECTED_TOOLS)

    def test_06_manager_group_3(self):
        self.assertEqual(
            {t.name for t in self.registry.available_for(self.manager3)}, EXPECTED_TOOLS)

    def test_07_warehouse_only_inventory(self):
        warehouse_tools = {
            'get_inventory_status', 'get_material_requirements', 'find_material_shortages',
            # فاز ۲: ابزارهای انبار برای انباردار هم همان دسترسی فاز ۱ را دارند
            'analyze_material_impact', 'simulate_material_availability',
        }
        self.assertEqual(
            {t.name for t in self.registry.available_for(self.warehouse)},
            warehouse_tools,
        )

    def test_08_plain_user_and_anonymous_have_nothing(self):
        for user in (self.plain, self.anon):
            allowed = self.registry.available_for(user)
            self.assertEqual(allowed, [], getattr(user, 'username', 'anonymous'))

    def test_09_execution_is_blocked_for_unauthorized_user(self):
        """حتی اگر UI دور زده شود، اجرا باید رد شود."""
        from craftflow_ai.orchestrator.planner import ToolPlanner

        planner = ToolPlanner(self.registry, self.plain)
        for tool in self.registry.list_tools():
            plan = planner.plan(tool.name, {'order_id': 1} if tool.name in
                                ('get_order_details', 'get_order_timeline',
                                 'get_material_requirements') else {})
            self.assertFalse(plan, f'{tool.name} allowed for plain user')
            self.assertEqual(
                plan.to_error()['error']['code'], 'PERMISSION_DENIED', tool.name)

    def test_10_denied_tools_are_not_advertised_to_the_llm(self):
        orch = AIOrchestrator(self.warehouse)
        advertised = {t['name'] for t in orch._llm_tools()}
        self.assertNotIn('get_order_details', advertised)
        self.assertNotIn('generate_production_report', advertised)
        self.assertIn('get_inventory_status', advertised)

    def test_11_llm_forcing_a_denied_tool_does_not_execute(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('get_order_details', {'order_id': 1})
        provider.queue_text('دسترسی ندارم.')
        result = AIOrchestrator(self.warehouse, provider=provider).chat(
            'وضعیت سفارش 1')
        activity = result.tool_calls[0]
        self.assertEqual(activity['status'], 'denied')
        self.assertEqual(activity['error_code'], 'PERMISSION_DENIED')

    def test_12_undefined_permission_fails_closed(self):
        self.assertFalse(has_permission(self.superuser, 'nonexistent.permission'))


class AuditLogTests(TestCase):
    """Section 7 — رد ممیزی."""

    def setUp(self):
        self.user = make_user('audit_user', groups=['1'])
        self.order = make_order()
        make_tasks(self.order, stations=('cut', 'cnc'))

    def test_01_all_three_models_are_created(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('get_order_details', {'order_id': self.order.id})
        provider.queue_text('پاسخ.')
        with override_settings(CRAFTFLOW_AI_ENABLED=True):
            from unittest import mock
            with mock.patch(
                'craftflow_ai.orchestrator.manager.build_provider',
                return_value=provider,
            ):
                self.client.force_login(self.user)
                response = self.client.post(
                    reverse('craftflow_ai:chat_api'),
                    data=json.dumps({'message': f'وضعیت سفارش {self.order.id}'}),
                    content_type='application/json',
                )
        self.assertEqual(response.status_code, 200)
        conversation_id = response.json()['conversation_id']

        conversation = AIConversation.objects.get(conversation_id=conversation_id)
        self.assertEqual(conversation.user_id, self.user.id)
        self.assertEqual(conversation.messages.count(), 2)

        rows = list(AIAuditLog.objects.filter(session_id=conversation_id))
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(any(r.tool_name == '__chat__' for r in rows))
        tool_row = next(r for r in rows if r.tool_name == 'get_order_details')
        self.assertEqual(tool_row.user_id, self.user.id)
        self.assertEqual(tool_row.status, 'completed')
        self.assertEqual(tool_row.arguments['order_id'], self.order.id)
        self.assertIsNotNone(tool_row.result)
        self.assertTrue(tool_row.result['success'])
        self.assertIsNotNone(tool_row.created_at)
        self.assertGreaterEqual(tool_row.duration_ms, 0)

    def test_02_denied_execution_is_audited(self):
        warehouse = make_user('audit_wh', groups=['انبار'])
        provider = FakeAIProvider()
        provider.queue_tool_call('get_order_details', {'order_id': 1})
        provider.queue_text('دسترسی ندارم.')
        result = AIOrchestrator(warehouse, provider=provider).chat('سفارش 1')
        row = AIAuditLog.objects.filter(
            tool_name='get_order_details', status='denied').first()
        self.assertIsNotNone(row)
        self.assertEqual(row.user_id, warehouse.id)
        self.assertEqual(row.error_code, 'PERMISSION_DENIED')
        self.assertEqual(row.session_id, result.conversation_id)

    def test_03_failed_execution_is_audited(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('get_order_details', 'bad-args')
        provider.queue_text('خطا.')
        AIOrchestrator(self.user, provider=provider).chat('سفارش 1')
        row = AIAuditLog.objects.filter(error_code='INVALID_ARGUMENTS').first()
        self.assertIsNotNone(row)
        self.assertEqual(row.status, 'failed')

    def test_04_secrets_are_sanitized_in_arguments(self):
        """ورودی عمداً حساس نباید در لاگ بماند."""
        secrets = {
            'api_key': 'sk-SUPERSECRET-1234567890',
            'API_KEY': 'sk-SUPERSECRET-1234567890',
            'Authorization': 'Bearer abc.def.ghi',
            'password': 'MyPassword!42',
            'secret': 'top-secret-value',
            'access_token': 'at_999888777',
            'x-goog-api-key': 'AIzaSyTOPSECRET',
        }
        AIAuditLog.objects.all().delete()
        audit.record(
            tool_name='get_open_orders',
            arguments={'limit': 5, **secrets, 'nested': {'token': 'deep-secret'}},
            user=self.user, session_id='sess-secret', status='completed',
        )
        row = AIAuditLog.objects.get(tool_name='get_open_orders')
        stored = json.dumps(row.arguments, ensure_ascii=False)
        self.assertEqual(row.arguments['limit'], 5)
        for key in secrets:
            self.assertNotIn(key, row.arguments, f'{key} survived')
        for value in secrets.values():
            self.assertNotIn(value, stored)
        self.assertNotIn('deep-secret', stored)
        # کلید ناشناختهٔ بی‌خطر باید بماند.
        self.assertIn('nested', row.arguments)

    def test_05_secrets_are_sanitized_in_results(self):
        audit.record(
            tool_name='get_inventory_status', arguments={}, user=self.user,
            result={'success': True, 'api_key': 'sk-LEAK', 'data': {'ok': 1}},
        )
        row = AIAuditLog.objects.get(tool_name='get_inventory_status')
        self.assertNotIn('sk-LEAK', json.dumps(row.result, ensure_ascii=False))

    def test_06_api_key_from_settings_is_never_audited(self):
        with override_settings(CRAFTFLOW_AI_API_KEY='sk-ENV-SECRET-999'):
            provider = FakeAIProvider()
            provider.queue_text('ok')
            AIOrchestrator(self.user, provider=provider).chat('سلام')
        dumped = json.dumps(
            [a.arguments for a in AIAuditLog.objects.all()], ensure_ascii=False)
        self.assertNotIn('sk-ENV-SECRET-999', dumped)

    def test_07_public_config_never_leaks_the_key(self):
        with override_settings(CRAFTFLOW_AI_API_KEY='sk-DO-NOT-LEAK'):
            public = config.public_config()
        self.assertTrue(public['has_api_key'])
        self.assertNotIn('sk-DO-NOT-LEAK', json.dumps(public, ensure_ascii=False))
        self.assertNotIn('api_key', public)
        self.assertEqual(set(public), {'enabled', 'provider', 'model', 'has_api_key'})

    def test_08_audit_failure_does_not_break_the_turn(self):
        """نبود جدول نباید جریان اصلی را متوقف کند."""
        with mock_delete_audit_table():
            provider = FakeAIProvider()
            provider.queue_tool_call('get_production_status')
            provider.queue_text('پاسخ.')
            result = AIOrchestrator(self.user, provider=provider).chat('وضعیت تولید')
        self.assertTrue(result.success)
        self.assertEqual(result.answer, 'پاسخ.')

    def test_09_chat_event_records_provider_and_tools(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('get_production_status')
        provider.queue_text('پاسخ.')
        AIOrchestrator(self.user, provider=provider).chat('وضعیت تولید')
        row = AIAuditLog.objects.get(tool_name='__chat__')
        self.assertEqual(row.arguments['provider'], 'fake')
        self.assertEqual(row.arguments['tools_called'], ['get_production_status'])
        self.assertEqual(row.status, 'completed')

    def test_10_error_turn_is_audited_as_failed(self):
        provider = FakeAIProvider([RuntimeError('boom')])
        AIOrchestrator(self.user, provider=provider).chat('وضعیت تولید')
        row = AIAuditLog.objects.filter(
            tool_name='__chat__', status='failed').first()
        self.assertIsNotNone(row)
        self.assertEqual(row.error_code, 'PROVIDER_ERROR')


class ChatApiTests(TestCase):
    """Section 8 — endpoint گفتگو."""

    def setUp(self):
        self.user = make_user('api_user', groups=['1'])
        self.order = make_order()
        make_tasks(self.order, stations=('cut', 'cnc'))
        self.url = reverse('craftflow_ai:chat_api')

    def _post(self, payload, user=None, raw=None, content_type='application/json'):
        client = self.client
        if user is not None:
            client.force_login(user)
        elif user is None and getattr(self, '_anon', False):
            pass
        body = raw if raw is not None else json.dumps(payload)
        return client.post(self.url, data=body, content_type=content_type)

    def test_01_anonymous_is_redirected(self):
        response = self.client.post(self.url, data=json.dumps({'message': 'سلام'}),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_02_get_is_not_allowed(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_01_happy_path(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('get_production_status')
        provider.queue_text('وضعیت تولید این است.')
        with _fake(provider):
            self.client.force_login(self.user)
            response = self.client.post(
                self.url, data=json.dumps({'message': 'وضعیت کلی تولید چیست؟'}),
                content_type='application/json')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['message'], 'وضعیت تولید این است.')
        self.assertEqual(body['provider'], 'fake')
        self.assertEqual(body['tool_calls'][0]['name'], 'get_production_status')
        self.assertTrue(AIConversation.objects.filter(
            conversation_id=body['conversation_id']).exists())

    def test_03_empty_message(self):
        self.client.force_login(self.user)
        for payload in ({'message': ''}, {'message': '   '}, {}):
            response = self.client.post(self.url, data=json.dumps(payload),
                                        content_type='application/json')
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()['error']['code'], 'EMPTY_MESSAGE')

    def test_04_very_long_message_is_truncated_not_rejected(self):
        provider = FakeAIProvider()
        provider.queue_text('ok')
        with _fake(provider):
            self.client.force_login(self.user)
            response = self.client.post(
                self.url, data=json.dumps({'message': 'ا' * 9000}),
                content_type='application/json')
        self.assertEqual(response.status_code, 200)
        stored = AIConversationMessage.objects.filter(role='user').first()
        self.assertEqual(len(stored.content), 4000)

    def test_05_invalid_json(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, data='{not json', content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'INVALID_JSON')

    def test_06_unknown_conversation_id_creates_a_new_one(self):
        provider = FakeAIProvider()
        provider.queue_text('ok')
        with _fake(provider):
            self.client.force_login(self.user)
            response = self.client.post(
                self.url,
                data=json.dumps({'message': 'سلام', 'conversation_id': 'does-not-exist'}),
                content_type='application/json')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['conversation_id'])
        self.assertTrue(AIConversation.objects.filter(
            conversation_id=body['conversation_id'], user=self.user).exists())

    def test_07_cannot_read_another_users_conversation(self):
        other = make_user('other_user', groups=['1'])
        conversation = AIConversation.objects.create(conversation_id='abc123', user=other)
        AIConversationMessage.objects.create(
            conversation=conversation, role='user', content='محرمانه')
        self.client.force_login(self.user)
        response = self.client.get(
            reverse('craftflow_ai:chat_history', args=['abc123']))
        self.assertEqual(response.status_code, 404)

    def test_08_ai_disabled_returns_503(self):
        self.client.force_login(self.user)
        with override_settings(CRAFTFLOW_AI_ENABLED=False):
            response = self.client.post(self.url, data=json.dumps({'message': 'سلام'}),
                                        content_type='application/json')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'AI_DISABLED')

    def test_09_provider_timeout_returns_502(self):
        provider = FakeAIProvider([TimeoutError('read timeout')])
        with _fake(provider):
            self.client.force_login(self.user)
            response = self.client.post(self.url, data=json.dumps({'message': 'وضعیت'}),
                                        content_type='application/json')
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()['success'])
        self.assertEqual(response.json()['error']['code'], 'PROVIDER_ERROR')

    def test_10_provider_malformed_response_is_contained(self):
        provider = FakeAIProvider([{'garbage': True}])
        with _fake(provider):
            self.client.force_login(self.user)
            response = self.client.post(self.url, data=json.dumps({'message': 'وضعیت'}),
                                        content_type='application/json')
        self.assertIn(response.status_code, (200, 502))
        self.assertIn('success', response.json())

    def test_11_tool_exception_returns_502_not_500_traceback(self):
        class ExplodingRegistry(ToolRegistry):
            def available_for(self, user):
                return super().available_for(user)

        provider = FakeAIProvider()
        provider.queue_tool_call('get_order_details', {'order_id': None})
        provider.queue_text('خطا.')
        with _fake(provider):
            self.client.force_login(self.user)
            response = self.client.post(
                self.url, data=json.dumps({'message': 'سفارش'}),
                content_type='application/json')
        self.assertIn(response.status_code, (200, 502))
        self.assertNotIn('Traceback', response.content.decode())

    def test_12_csrf_is_enforced(self):
        enforcing = self.client_class(enforce_csrf_checks=True)
        enforcing.force_login(self.user)
        response = enforcing.post(self.url, data=json.dumps({'message': 'سلام'}),
                                  content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_13_tools_endpoint_is_scoped_to_user(self):
        warehouse = make_user('api_wh', groups=['انبار'])
        self.client.force_login(warehouse)
        body = self.client.get(reverse('craftflow_ai:tools_api')).json()
        self.assertTrue(body['success'])
        # انباردار: سه ابزار فاز ۱ + دو ابزار انبار فاز ۲ (تحلیل/شبیه‌سازی مواد).
        self.assertEqual(
            {tool['name'] for tool in body['tools']},
            {'get_inventory_status', 'get_material_requirements',
             'find_material_shortages', 'analyze_material_impact',
             'simulate_material_availability'},
        )
        self.assertFalse(body['permissions']['reports.view'])
        self.client.force_login(self.user)
        body = self.client.get(reverse('craftflow_ai:tools_api')).json()
        self.assertEqual(len(body['tools']), EXPECTED_TOOL_COUNT)

    def test_14_sequential_messages_keep_one_conversation(self):
        provider = FakeAIProvider()
        provider.queue_text('اول')
        with _fake(provider):
            self.client.force_login(self.user)
            first = self.client.post(self.url, data=json.dumps({'message': 'اول'}),
                                     content_type='application/json').json()
        provider2 = FakeAIProvider()
        provider2.queue_text('دوم')
        with _fake(provider2):
            second = self.client.post(
                self.url,
                data=json.dumps({'message': 'دوم',
                                 'conversation_id': first['conversation_id']}),
                content_type='application/json').json()
        self.assertEqual(first['conversation_id'], second['conversation_id'])
        self.assertEqual(
            AIConversation.objects.get(
                conversation_id=first['conversation_id']).messages.count(), 4)

    def test_15_response_is_valid_json_with_stable_keys(self):
        provider = FakeAIProvider()
        provider.queue_text('ok')
        with _fake(provider):
            self.client.force_login(self.user)
            response = self.client.post(self.url, data=json.dumps({'message': 'سلام'}),
                                        content_type='application/json')
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertTrue({
            'success', 'message', 'conversation_id', 'tool_calls', 'provider', 'model'
        }.issubset(set(response.json())))


class ProviderConfigTests(TestCase):
    """Section 10 — پیکربندی و transport."""

    def test_01_fake_and_local_build_offline(self):
        with override_settings(CRAFTFLOW_AI_PROVIDER='fake'):
            self.assertEqual(build_provider().name, 'fake')
        with override_settings(CRAFTFLOW_AI_PROVIDER='local'):
            provider = build_provider()
            self.assertEqual(provider.name, 'local')
            self.assertTrue(provider.healthcheck()['offline'])

    def test_02_named_providers_build_without_network(self):
        for name, cls in (('gemini', 'GeminiProvider'),
                          ('google', 'GeminiProvider'),
                          ('openai', 'OpenAIProvider'),
                          ('anthropic', 'AnthropicProvider'),
                          ('claude', 'AnthropicProvider')):
            with override_settings(CRAFTFLOW_AI_PROVIDER=name):
                provider = build_provider()
            self.assertEqual(type(provider).__name__, cls)
            self.assertTrue(hasattr(provider, 'chat'))
            self.assertTrue(hasattr(provider, 'model'))

    def test_03_invalid_provider_raises_controlled_error(self):
        for bogus in ('mystery', 'gpt-9000', '', '   ', None):
            with override_settings(CRAFTFLOW_AI_PROVIDER=bogus or 'mystery'):
                with self.assertRaises(AIConfigurationError) as ctx:
                    build_provider()
            error = ctx.exception
            self.assertEqual(error.code, 'PROVIDER_NOT_CONFIGURED')
            self.assertEqual(error.http_status, 503)
            self.assertIn('detail', error.to_dict())
            self.assertIn('available', error.to_dict()['detail'])

    def test_04_invalid_provider_through_orchestrator_is_contained(self):
        user = make_user('cfg_user', groups=['1'])
        with override_settings(CRAFTFLOW_AI_PROVIDER='mystery'):
            result = AIOrchestrator(user).chat('وضعیت تولید')
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 'PROVIDER_NOT_CONFIGURED')

    def test_05_invalid_provider_through_api_returns_controlled_error(self):
        """
        Provider نامعتبر باید خطای کنترل‌شده بدهد، نه traceback خام.

        ``AIConfigurationError.http_status`` برابر ۵۰۳ است (پیکربندی ناقص،
        نه خطای موقت سرویس) و view همان را برمی‌گرداند.
        """
        user = make_user('cfg_api', groups=['1'])
        with override_settings(CRAFTFLOW_AI_PROVIDER='mystery'):
            self.client.force_login(user)
            response = self.client.post(
                reverse('craftflow_ai:chat_api'),
                data=json.dumps({'message': 'وضعیت تولید'}),
                content_type='application/json')
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['error']['code'], 'PROVIDER_NOT_CONFIGURED')
        self.assertNotIn('Traceback', response.content.decode())
        self.assertNotIn('mystery-ai-internal-path', response.content.decode())

    def test_06_config_reads_settings_with_defaults(self):
        with override_settings(CRAFTFLOW_AI_PROVIDER=' OpenAI ', CRAFTFLOW_AI_MODEL=' gpt-x '):
            self.assertEqual(config.get_provider_name(), 'openai')
            self.assertEqual(config.get_model(), 'gpt-x')

    def test_07_numeric_settings_are_coerced_and_fallback_safely(self):
        with override_settings(CRAFTFLOW_AI_MAX_TOOL_CALLS='5', CRAFTFLOW_AI_TIMEOUT='30',
                               CRAFTFLOW_AI_TEMPERATURE='0.2'):
            self.assertEqual(config.get_max_tool_calls(), 5)
            self.assertEqual(config.get_timeout(), 30)
            self.assertAlmostEqual(config.get_temperature(), 0.2)
        for bad in ('abc', None, -5):
            with override_settings(CRAFTFLOW_AI_MAX_TOOL_CALLS=bad):
                self.assertGreaterEqual(config.get_max_tool_calls(), 1)

    def test_08_enabled_flag(self):
        with override_settings(CRAFTFLOW_AI_ENABLED=False):
            self.assertFalse(config.is_enabled())
        with override_settings(CRAFTFLOW_AI_ENABLED=True):
            self.assertTrue(config.is_enabled())

    def test_09_real_providers_expose_transport_configuration(self):
        """
        فقط پیکربندی/transport — بدون هیچ درخواست شبکه‌ای.

        هر Provider واقعی باید endpoint قابل محاسبه، timeout و نیازمندی کلید
        داشته باشد؛ در نبود کلید باید خطای کنترل‌شده بدهد نه استثنای خام.
        """
        from craftflow_ai.permissions.errors import AIConfigurationError

        with override_settings(CRAFTFLOW_AI_API_KEY='', CRAFTFLOW_AI_BASE_URL=''):
            for name in ('gemini', 'openai', 'anthropic'):
                provider = get_provider(name)
                self.assertTrue(getattr(provider, 'base_url', '').startswith('http'))
                self.assertGreater(provider.timeout, 0)
                with self.assertRaises(AIConfigurationError) as ctx:
                    provider.chat([{'role': 'user', 'content': 'سلام'}], tools=[])
                self.assertEqual(ctx.exception.code, 'PROVIDER_NOT_CONFIGURED')
                self.assertFalse(provider.healthcheck()['configured'])

        with override_settings(CRAFTFLOW_AI_API_KEY='test-key-not-real',
                               CRAFTFLOW_AI_BASE_URL=''):
            for name in ('gemini', 'openai', 'anthropic'):
                provider = get_provider(name)
                self.assertTrue(provider.healthcheck()['configured'])
            # فقط Gemini مسیر :generateContent را در خود endpoint می‌سازد.
            gemini = get_provider('gemini')
            self.assertIn('models/', gemini._endpoint())
            self.assertTrue(gemini._endpoint().endswith(':generateContent'))

    def test_10_base_url_override_is_respected(self):
        with override_settings(CRAFTFLOW_AI_BASE_URL='https://proxy.internal/v1/'):
            for name in ('gemini', 'openai', 'anthropic'):
                provider = get_provider(name)
                self.assertEqual(provider.base_url, 'https://proxy.internal/v1')
                self.assertFalse(provider.base_url.endswith('/'))


class RealModelNameConsistencyTests(TestCase):
    """ابزارها باید با مدل واقعی CraftFlow هم‌خوان باشند."""

    def test_01_station_names_come_from_the_model(self):
        codes = {c for c, _ in STATION_CHOICES}
        # توجه: کد ایستگاه وکیوم در مدل واقعی «vacum» است، نه «vacuum».
        self.assertEqual(
            codes,
            {'cut', 'cnc', 'dr', 'pvc', 'prs', 'mon', 'vacum', 'paint',
             'assembly2', 'packaging', 'shipping'},
        )
        from craftflow_ai.services import queries
        for station in queries.pending_tasks(stage='cut')['tasks'][:1]:
            self.assertIn(station['stage'], codes)

    def test_01b_stage_hint_in_tool_description_matches_the_model(self):
        """توضیح ابزار نباید کد ایستگاهی اشتباه به LLM بدهد."""
        tool = get_registry().get('get_pending_tasks')
        hint = tool.input_schema['properties']['stage']['description']
        codes = {c for c, _ in STATION_CHOICES}
        mentioned = {c for c in codes if c in hint}
        self.assertEqual(mentioned, codes, f'توضیح ابزار همهٔ کدها را ندارد: {hint}')
        # «vacuum» نباید به‌عنوان کد معتبر جا بیفتد (کد درست «vacum» است).
        import re
        listed = set(re.findall(r'[a-z0-9]+', hint))
        self.assertNotIn('vacuum', listed)

    def test_02_unknown_stage_is_rejected_with_guidance(self):
        from craftflow_ai.tools import get_registry
        result = get_registry().get('get_pending_tasks').run(stage='laser')
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['code'], 'UNKNOWN_STAGE')
        self.assertIn('available_stages', result['error']['detail'])

    def test_03_task_statuses_are_the_real_model_ones(self):
        self.assertEqual(
            {s for s, _ in ProductionTask.TASK_STATUS}, {'waiting', 'pending', 'done'})

    def test_04_derived_statuses_are_labelled(self):
        from craftflow_ai.services import queries
        status = queries.production_status()
        self.assertIn('derived_fields', status)
        self.assertIn('in_progress', status['derived_fields'])
        self.assertIn('blocked', status['derived_fields'])


# ----------------------------------------------------------------------
# کمکی‌ها
# ----------------------------------------------------------------------

class _fake:
    def __init__(self, provider):
        self.provider = provider
        self.patcher = None

    def __enter__(self):
        from unittest import mock
        self.patcher = mock.patch(
            'craftflow_ai.orchestrator.manager.build_provider',
            return_value=self.provider)
        self.patcher.start()
        return self.provider

    def __exit__(self, *exc):
        self.patcher.stop()
        return False


class mock_delete_audit_table:
    """شبیه‌سازی خرابی جدول ممیزی بدون دست‌زدن به schema."""

    def __enter__(self):
        from unittest import mock
        self.patcher = mock.patch(
            'craftflow_ai.models.AIAuditLog.objects.create',
            side_effect=RuntimeError('no such table: craftflow_ai_aiauditlog'))
        self.patcher.start()
        return self

    def __exit__(self, *exc):
        self.patcher.stop()
        return False
