"""
تست یکپارچهٔ کامل:

    User → Chat API → Orchestrator → Tool → Django Service/ORM → Database
         → LLM (FakeAIProvider) → Response

هیچ تماس شبکه‌ای و هیچ API Key ای در این تست انجام نمی‌شود؛ نقش LLM را
``FakeAIProvider`` بازی می‌کند، اما Orchestrator، Planner، Permission،
ابزار، سرویس‌های فقط‌خواندنی و دیتابیس همگی واقعی هستند.
"""
import json
from decimal import Decimal

import jdatetime
from django.test import TestCase
from django.urls import reverse

from craftflow_ai.models import AIAuditLog, AIConversation, AIConversationMessage
from craftflow_ai.orchestrator.manager import AIOrchestrator
from craftflow_ai.providers.local import FakeAIProvider
from craftflow_ai.permissions.errors import AITimeoutError, AIProviderError

from .factories import (
    PASSWORD,
    make_order,
    make_raw_material,
    make_tasks,
    make_user,
)


class ChatIntegrationTests(TestCase):
    """مسیر کامل از HTTP تا دیتابیس و برگشت."""

    def setUp(self):
        self.manager = make_user('manager', groups=['2'])
        self.client.login(username='manager', password=PASSWORD)

    def post_chat(self, message, conversation_id=None, provider=None):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        return self.client.post(
            reverse('craftflow_ai:chat_api'),
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_full_flow_with_fake_provider(self):
        """کاربر می‌پرسد، Orchestrator ابزار را اجرا می‌کند، LLM پاسخ می‌دهد."""
        order = make_order()
        make_tasks(order, stations=('cut', 'cnc', 'mon'))

        provider = FakeAIProvider()
        provider.queue_tool_call('get_production_status')
        provider.queue_text('ایستگاه مونتاژ بیشترین تراکم را دارد.')

        with self.settings(CRAFTFLOW_AI_PROVIDER='fake'):
            response = self._post_with_provider(provider, 'وضعیت تولید چطور است؟')

        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertTrue(data['success'])
        self.assertEqual(data['message'], 'ایستگاه مونتاژ بیشترین تراکم را دارد.')
        self.assertTrue(data['conversation_id'])
        self.assertEqual(len(data['tool_calls']), 1)
        call = data['tool_calls'][0]
        self.assertEqual(call['name'], 'get_production_status')
        self.assertEqual(call['status'], 'completed')
        # برچسب فارسی برای UI، بدون آرگومان و خروجی خام
        self.assertEqual(call['label'], 'وضعیت تولید')

        # ممیزی ثبت شده است
        audit = AIAuditLog.objects.filter(tool_name='get_production_status').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.status, 'completed')
        self.assertEqual(audit.user_id, self.manager.id)

        # گفتگو و پیام‌ها ذخیره شده‌اند
        conversation = AIConversation.objects.get(
            conversation_id=data['conversation_id']
        )
        self.assertEqual(conversation.user_id, self.manager.id)
        self.assertEqual(conversation.messages.count(), 2)

    def _post_with_provider(self, provider, message):
        """POST با Provider تزریق‌شده (تست بدون شبکه)."""
        import craftflow_ai.views as ai_views

        original = ai_views.AIOrchestrator

        class Patched(original):
            def __init__(self, user, *args, **kwargs):
                super().__init__(user, provider=provider, *args, **kwargs)

        ai_views.AIOrchestrator = Patched
        try:
            return self.post_chat(message)
        finally:
            ai_views.AIOrchestrator = original

    def test_order_details_flow_reaches_database(self):
        """پاسخ مدل باید بتواند به دادهٔ واقعی سفارش تکیه کند."""
        order = make_order(customer_name='مشتری تست یکپارچه')
        make_tasks(order, stations=('cut', 'cnc'))

        provider = FakeAIProvider()
        provider.queue_tool_call('get_order_details', {'order_id': order.id})
        provider.queue_text('سفارش در حال تولید است.')

        with self.settings(CRAFTFLOW_AI_PROVIDER='fake'):
            response = self._post_with_provider(provider, f'وضعیت سفارش {order.id} چیست؟')

        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['tool_calls'][0]['name'], 'get_order_details')
        self.assertEqual(data['tool_calls'][0]['arguments']['order_id'], order.id)

        # نتیجهٔ ابزار واقعاً به مدل رسیده و شامل دادهٔ واقعی است
        tool_messages = [
            m for m in provider.calls[-1]['messages'] if m['role'] == 'tool'
        ]
        self.assertTrue(tool_messages)
        payload = json.loads(tool_messages[-1]['content'])
        self.assertTrue(payload['success'])
        self.assertEqual(payload['data']['order_id'], order.id)
        self.assertEqual(payload['data']['customer'], 'مشتری تست یکپارچه')

    def test_inventory_shortage_flow(self):
        order = make_order()
        make_tasks(order, stations=('cut',))
        raw = make_raw_material(name='رنگ قرمز', stock=Decimal('1'), min_alert=Decimal('0'))
        from craftflow_ai.tests.factories import make_open_issue
        make_open_issue(raw, order, quantity=Decimal('10'))

        provider = FakeAIProvider()
        provider.queue_tool_call('find_material_shortages')
        provider.queue_text('رنگ قرمز کمبود دارد.')

        with self.settings(CRAFTFLOW_AI_PROVIDER='fake'):
            response = self._post_with_provider(provider, 'کدام مواد کم داریم؟')

        data = response.json()
        self.assertTrue(data['success'])
        tool_payload = json.loads(provider.calls[-1]['messages'][-1]['content'])
        self.assertTrue(tool_payload['data']['shortage_count'] >= 1)
        shortage = tool_payload['data']['shortages'][0]
        self.assertEqual(shortage['raw_material'], 'رنگ قرمز')
        self.assertIn('نیاز فیزیکی', shortage['reason'])

    def test_ai_disabled_returns_503(self):
        with self.settings(CRAFTFLOW_AI_ENABLED=False):
            response = self.post_chat('وضعیت تولید؟')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'AI_DISABLED')

    def test_empty_message_returns_400(self):
        response = self.post_chat('   ')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'EMPTY_MESSAGE')

    def test_invalid_json_returns_400(self):
        response = self.client.post(
            reverse('craftflow_ai:chat_api'),
            data='{not json',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'INVALID_JSON')

    def test_anonymous_user_is_redirected(self):
        self.client.logout()
        response = self.post_chat('وضعیت تولید؟')
        self.assertIn(response.status_code, (302, 403))

    def test_get_request_not_allowed(self):
        response = self.client.get(reverse('craftflow_ai:chat_api'))
        self.assertEqual(response.status_code, 405)


class OrchestratorErrorTests(TestCase):
    """خطاهای LLM باید به کاربر پیام روشن بدهد، نه crash."""

    def setUp(self):
        self.manager = make_user('mgr', groups=['2'])

    def test_provider_timeout_is_handled(self):
        provider = FakeAIProvider([AITimeoutError('اتمام زمان')])
        result = AIOrchestrator(self.manager, provider=provider).chat('وضعیت تولید؟')

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 'PROVIDER_TIMEOUT')
        self.assertIn('زمان', result.answer)

    def test_provider_generic_error_is_handled(self):
        provider = FakeAIProvider([AIProviderError('خطای سرویس')])
        result = AIOrchestrator(self.manager, provider=provider).chat('وضعیت تولید؟')

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 'PROVIDER_ERROR')

    def test_unknown_provider_raises_configuration_error(self):
        from craftflow_ai.permissions.errors import AIConfigurationError
        from craftflow_ai.providers import build_provider

        with self.settings(CRAFTFLOW_AI_PROVIDER='does-not-exist'):
            with self.assertRaises(AIConfigurationError):
                build_provider()

    def test_invalid_provider_reports_available_names(self):
        from craftflow_ai.permissions.errors import AIConfigurationError
        from craftflow_ai.providers import build_provider

        with self.assertRaises(AIConfigurationError) as ctx:
            build_provider('bogus-provider')
        self.assertIn('gemini', ctx.exception.detail['available'])

    def test_max_tool_calls_is_enforced(self):
        """مدلی که بی‌پایان ابزار صدا می‌زند باید متوقف شود."""
        provider = FakeAIProvider()
        for _ in range(20):
            provider.queue_tool_call('get_production_status')

        with self.settings(CRAFTFLOW_AI_MAX_TOOL_CALLS=2):
            result = AIOrchestrator(self.manager, provider=provider).chat('گزارش بده')

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 'MAX_TOOL_CALLS_EXCEEDED')
        self.assertLessEqual(len(result.tool_calls), 2)

    def test_provider_without_api_key_is_reported(self):
        from craftflow_ai.permissions.errors import AIConfigurationError
        from craftflow_ai.providers.gemini import GeminiProvider

        provider = GeminiProvider(api_key='', model='gemini-2.5-flash')
        with self.assertRaises(AIConfigurationError) as ctx:
            provider.chat([{'role': 'user', 'content': 'سلام'}], tools=[])
        self.assertEqual(ctx.exception.detail['missing'], 'CRAFTFLOW_AI_API_KEY')


class OrchestratorToolSafetyTests(TestCase):
    """مدل نباید بتواند ابزار غیرمجاز یا ناشناخته را اجرا کند."""

    def setUp(self):
        self.manager = make_user('mgr', groups=['2'])
        self.warehouse = make_user('wh', groups=['انبار'])

    def test_llm_cannot_call_unknown_tool(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('definitely_not_a_real_tool')
        provider.queue_text('متوجه شدم.')

        result = AIOrchestrator(self.manager, provider=provider).chat('یه کاری بکن')

        self.assertEqual(result.tool_calls[0]['status'], 'failed')
        self.assertEqual(result.tool_calls[0]['error_code'], 'TOOL_NOT_FOUND')
        # مدل نتیجهٔ خطا را دریافت می‌کند
        payload = json.loads(provider.calls[-1]['messages'][-1]['content'])
        self.assertFalse(payload['success'])

    def test_llm_cannot_bypass_permissions(self):
        """کاربر انبار نباید بتواند با فراخوانی مستقیم ابزار تولید، دور بزند."""
        provider = FakeAIProvider()
        provider.queue_tool_call('get_production_status')
        provider.queue_text('دسترسی ندارم.')

        result = AIOrchestrator(self.warehouse, provider=provider).chat('وضعیت تولید')

        self.assertEqual(result.tool_calls[0]['status'], 'denied')
        self.assertEqual(result.tool_calls[0]['error_code'], 'PERMISSION_DENIED')

        # ابزار به LLM اصلاً معرفی نشده بود
        first_call_tool_names = {t['name'] for t in provider.calls[0]['tools']}
        self.assertNotIn('get_production_status', first_call_tool_names)

        audit = AIAuditLog.objects.filter(tool_name='get_production_status').first()
        self.assertEqual(audit.status, 'denied')

    def test_llm_cannot_execute_sql_like_tool(self):
        provider = FakeAIProvider()
        provider.queue_tool_call('execute_sql', {'query': 'DROP TABLE product_order'})
        provider.queue_text('چنین ابزاری وجود ندارد.')

        result = AIOrchestrator(self.manager, provider=provider).chat('پایگاه‌داده را خالی کن')

        self.assertEqual(result.tool_calls[0]['error_code'], 'TOOL_NOT_FOUND')
        # هیچ جدولی لمس نشده است
        from product.models import Order
        self.assertEqual(Order.objects.count(), 0)

    def test_write_request_is_refused_by_system_design(self):
        """در فاز ۱ هیچ ابزار نوشتنی وجود ندارد، پس درخواست اجراپذیر نیست."""
        provider = FakeAIProvider()
        provider.queue_tool_call('create_production_tasks', {'order_id': 1})
        provider.queue_text('این عملیات در نسخهٔ فعلی قابل اجرا نیست.')

        result = AIOrchestrator(self.manager, provider=provider).chat(
            'سفارش را وارد تولید کن'
        )
        self.assertEqual(result.tool_calls[0]['error_code'], 'TOOL_NOT_FOUND')

        from product.models import ProductionTask
        self.assertEqual(ProductionTask.objects.count(), 0)


class ConversationFlowTests(TestCase):

    def setUp(self):
        self.manager = make_user('mgr2', groups=['2'])
        self.client.login(username='mgr2', password=PASSWORD)

    def _chat(self, provider, message, conversation_id=None):
        import craftflow_ai.views as ai_views
        original = ai_views.AIOrchestrator

        class Patched(original):
            def __init__(self, user, *args, **kwargs):
                super().__init__(user, provider=provider, *args, **kwargs)

        ai_views.AIOrchestrator = Patched
        try:
            payload = {'message': message}
            if conversation_id:
                payload['conversation_id'] = conversation_id
            return self.client.post(
                reverse('craftflow_ai:chat_api'),
                data=json.dumps(payload),
                content_type='application/json',
            )
        finally:
            ai_views.AIOrchestrator = original

    def test_conversation_history_is_carried_to_llm(self):
        provider = FakeAIProvider()
        provider.queue_text('پاسخ اول')
        first = self._chat(provider, 'سلام').json()
        conversation_id = first['conversation_id']

        provider.queue_tool_call('get_open_orders')
        provider.queue_text('پاسخ دوم')
        second = self._chat(provider, 'حالا سفارش‌ها', conversation_id).json()

        self.assertEqual(second['conversation_id'], conversation_id)
        self.assertEqual(AIConversation.objects.count(), 1)

        # پیام قبلی کاربر به مدل رسیده است
        roles = [m['role'] for m in provider.calls[-1]['messages']]
        self.assertIn('user', roles)

    def test_cannot_read_another_users_conversation(self):
        other = make_user('other', groups=['2'])
        conversation = AIConversation.objects.create(
            conversation_id='secret-conv', user=other,
        )
        AIConversationMessage.objects.create(
            conversation=conversation, role='user', content='پیام خصوصی',
        )

        response = self.client.get(
            reverse('craftflow_ai:chat_history', args=['secret-conv'])
        )
        self.assertEqual(response.status_code, 404)

    def test_failed_turn_is_not_saved_as_conversation(self):
        provider = FakeAIProvider([AITimeoutError('اتمام زمان')])
        response = self._chat(provider, 'وضعیت تولید؟')

        # AITimeoutError.http_status = 504 است و view باید همان را برگرداند
        # (نه ۵۰۲ عمومی) تا معنای HTTP درست بماند.
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()['error']['code'], 'PROVIDER_TIMEOUT')
        self.assertFalse(response.json()['success'])
        self.assertEqual(AIConversationMessage.objects.count(), 0)


class ChatViewTests(TestCase):

    def test_chat_page_renders_for_manager(self):
        make_user('mgr3', groups=['2'])
        self.client.login(username='mgr3', password=PASSWORD)
        response = self.client.get(reverse('craftflow_ai:chat'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'دستیار هوشمند')
        self.assertContains(response, reverse('craftflow_ai:chat_api'))

    def test_chat_page_requires_login(self):
        response = self.client.get(reverse('craftflow_ai:chat'))
        self.assertEqual(response.status_code, 302)

    def test_tools_api_lists_only_allowed_tools(self):
        make_user('wh2', groups=['انبار'])
        self.client.login(username='wh2', password=PASSWORD)
        response = self.client.get(reverse('craftflow_ai:tools_api'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        names = {t['name'] for t in data['tools']}
        self.assertIn('get_inventory_status', names)
        self.assertNotIn('get_production_status', names)
        self.assertTrue(all(t['read_only'] for t in data['tools']))


class AuditLogTests(TestCase):

    def test_audit_records_tool_arguments_and_summary(self):
        order = make_order()
        manager = make_user('mgr4', groups=['2'])
        provider = FakeAIProvider()
        provider.queue_tool_call('get_order_details', {'order_id': order.id})
        provider.queue_text('پاسخ')

        AIOrchestrator(manager, provider=provider).chat(f'سفارش {order.id}')

        entry = AIAuditLog.objects.filter(tool_name='get_order_details').first()
        self.assertEqual(entry.status, 'completed')
        self.assertEqual(entry.arguments['order_id'], order.id)
        self.assertTrue(entry.result['success'])

    def test_audit_never_stores_api_keys(self):
        from craftflow_ai.audit.logger import record

        record(
            tool_name='test_tool',
            arguments={'order_id': 1, 'api_key': 'SECRET-SHOULD-NOT-PERSIST'},
            user=None,
            status='completed',
        )
        entry = AIAuditLog.objects.get(tool_name='test_tool')
        self.assertNotIn('api_key', entry.arguments)
        self.assertEqual(entry.arguments['order_id'], 1)

    def test_chat_turn_is_audited(self):
        manager = make_user('mgr5', groups=['2'])
        provider = FakeAIProvider()
        provider.queue_tool_call('get_production_status')
        provider.queue_text('پاسخ')

        AIOrchestrator(manager, provider=provider).chat('وضعیت تولید')

        entry = AIAuditLog.objects.filter(tool_name='__chat__').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, 'completed')
        self.assertIn('get_production_status', entry.arguments['tools_called'])
        self.assertEqual(entry.user_id, manager.id)

    def test_migrations_are_in_sync(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command('makemigrations', 'craftflow_ai', '--check', '--dry-run', stdout=out)
        self.assertIn('No changes detected', out.getvalue())