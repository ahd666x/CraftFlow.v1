"""
Orchestrator — قلب لایهٔ AI.

جریان کار:

    پیام کاربر
        ↓
    ساخت Context (تاریخ، کاربر، دسترسی‌ها، خلاصهٔ کارخانه)
        ↓
    ارسال به LLM همراه با شمای ابزارهای مجاز
        ↓
    ┌─ مدل ابزار خواست؟ ─┐
    │                    ↓
    │            بررسی دسترسی (authorize)
    │                    ↓
    │            اجرای ابزار از رجیستری (سفارشی‌شده)
    │                    ↓
    │            ثبت ممیزی
    │                    ↓
    │            بازگرداندن نتیجه به مدل  ──┐
    └──────────────────────────────┐       │ (تا سقف max_tool_calls)
                                   ↓       │
                              پاسخ نهایی ←──┘

قواعد امنیتی اعمال‌شده در همین فایل:
  * فقط ابزارهای مجاز کاربر به مدل معرفی می‌شوند (available_for).
  * حتی اگر مدل نام ابزاری خارج از رجیستری یا خارج از دسترس را بسازد،
    اجرا با ``authorize`` + ``registry.get`` متوقف می‌شود.
  * سقف تعداد فراخوانی ابزار وجود دارد تا یک حلقهٔ بی‌پایان ممکن نباشد.
  * هیچ Providerی به ORM دسترسی ندارد؛ فقط از مسیر همین فایل داده می‌گیرد.
"""
import json
import logging
import time
import uuid

from craftflow_ai import config
from craftflow_ai.audit import logger as audit
from craftflow_ai.orchestrator.context import ContextBuilder
from craftflow_ai.orchestrator.planner import ToolPlanner
from craftflow_ai.orchestrator.prompts import get_system_prompt
from craftflow_ai.permissions.errors import AIDisabledError, AIToolError
from craftflow_ai.providers import build_provider

logger = logging.getLogger('craftflow_ai.orchestrator')


class OrchestratorResult:
    """خروجی یک نوبت گفتگو، آمادهٔ تبدیل به JSON یا نمایش در UI."""

    def __init__(self, answer, tool_calls, provider, model, conversation_id,
                 error=None, error_code=''):
        self.answer = answer
        self.tool_calls = tool_calls
        self.provider = provider
        self.model = model
        self.conversation_id = conversation_id
        self.error = error
        self.error_code = error_code

    @property
    def success(self):
        return self.error is None

    def to_dict(self):
        payload = {
            'success': self.error is None,
            'message': self.answer,
            'conversation_id': self.conversation_id,
            'tool_calls': self.tool_calls,
            'provider': self.provider,
            'model': self.model,
        }
        if self.error:
            payload['error'] = {'code': self.error_code or 'AI_ERROR', 'message': self.error}
        return payload


class AIOrchestrator:
    """هماهنگ‌کنندهٔ گفتگو، ابزار و LLM."""

    def __init__(self, user, provider=None, registry=None, enabled=None):
        self.user = user
        self._provider = provider
        self._registry = registry
        self.enabled = config.is_enabled() if enabled is None else enabled
        self.context_builder = ContextBuilder(user, registry=registry)

    # -- وابستگی‌ها --------------------------------------------------
    @property
    def registry(self):
        if self._registry is None:
            from craftflow_ai.tools import get_registry
            self._registry = get_registry()
        return self._registry

    @property
    def provider(self):
        if self._provider is None:
            self._provider = build_provider()
        return self._provider

    def _provider_identity(self):
        """
        نام و مدل Provider بدون آنکه ساخت آن دوباره exception بدهد.

        اگر پیکربندی Provider نادرست باشد، خطا باید داخل چرخهٔ گفتگو مدیریت شود،
        نه اینکه از ``chat`` به‌صورت traceback خام بیرون بزند.
        """
        try:
            provider = self.provider
        except AIToolError as exc:
            return exc.code, ''
        return getattr(provider, 'name', ''), getattr(provider, 'model', '')

    # -- نقطهٔ ورود --------------------------------------------------
    def chat(self, message, conversation_id=None, history=None):
        conversation_id = conversation_id or uuid.uuid4().hex
        message = (message or '').strip()

        if not self.enabled:
            raise AIDisabledError(
                'دستیار هوشمند CraftFlow غیرفعال است.',
                detail={'setting': 'CRAFTFLOW_AI_ENABLED'},
            )
        if not message:
            return OrchestratorResult(
                answer='', tool_calls=[], provider='', model='',
                conversation_id=conversation_id,
                error='پیام خالی است.', error_code='EMPTY_MESSAGE',
            )

        started = time.monotonic()
        tool_calls = []
        tools = self._llm_tools()

        try:
            context_block = self.context_builder.build(
                user_message=message,
                tool_results=[],
                conversation_history=history or [],
            )
        except Exception:
            logger.exception('Context build failed')
            context_block = {'user_message': message}

        messages = self._build_messages(message, context_block, tool_calls)

        max_calls = config.get_max_tool_calls()
        final_text = ''
        error = None
        error_code = ''

        for iteration in range(max_calls + 1):
            try:
                response = self.provider.chat(
                    messages=messages,
                    tools=tools,
                    temperature=config.get_temperature(),
                )
            except AIToolError as exc:
                error = exc.message
                error_code = exc.code
                break
            except Exception as exc:
                logger.exception('Provider %s failed', getattr(self.provider, 'name', '?'))
                error = 'ارتباط با سرویس هوش مصنوعی برقرار نشد.'
                error_code = 'PROVIDER_ERROR'
                break

            if not response.wants_tools:
                final_text = response.content
                break

            if iteration >= max_calls:
                error = (
                    f'سقف {max_calls} فراخوانی ابزار در یک پاسخ رد شد؛ '
                    'لطفاً پرسش را دقیق‌تر بیان کنید.'
                )
                error_code = 'MAX_TOOL_CALLS_EXCEEDED'
                break

            for call in response.tool_calls:
                outcome = self._execute_tool_call(
                    tool_name=call.name,
                    arguments=call.arguments,
                    conversation_id=conversation_id,
                    tool_calls=tool_calls,
                )
                messages.append({
                    'role': 'tool',
                    'content': json.dumps(outcome, ensure_ascii=False),
                })

        provider_name, model_name = self._provider_identity()

        if error and not final_text:
            final_text = error

        audit.record_conversation_event(
            user=self.user,
            conversation_id=conversation_id,
            provider=provider_name,
            model=model_name,
            tool_names=[c['name'] for c in tool_calls],
            error_code=error_code,
        )

        logger.info(
            'AI turn conversation=%s user=%s provider=%s tools=%s duration_ms=%d error=%s',
            conversation_id,
            getattr(self.user, 'pk', None),
            provider_name,
            [c['name'] for c in tool_calls],
            int((time.monotonic() - started) * 1000),
            error_code or '-',
        )

        return OrchestratorResult(
            answer=final_text,
            tool_calls=tool_calls,
            provider=provider_name,
            model=model_name,
            conversation_id=conversation_id,
            error=error,
            error_code=error_code,
        )

    # -- ابزار -------------------------------------------------------
    def _llm_tools(self):
        """فقط ابزارهایی که کاربر واقعاً اجازهٔ اجرایشان را دارد."""
        return [tool.to_schema() for tool in self.registry.available_for(self.user)]

    def _execute_tool_call(self, tool_name, arguments, conversation_id, tool_calls):
        """
        اجرای یک فراخوانی ابزار از سمت LLM.

        هر شکست به‌صورت یک نتیجهٔ ساختاریافته به مدل برمی‌گردد (نه exception)،
        تا مدل بتواند خطا را به کاربر توضیح دهد. اما اجرای خارج از دسترس یا
        ناشناخته هرگز انجام نمی‌شود.
        """
        started = time.monotonic()
        activity = {
            'name': tool_name,
            'label': tool_name,
            'arguments': arguments if isinstance(arguments, dict) else {},
            'status': 'failed',
        }

        if not isinstance(arguments, dict):
            payload = {
                'success': False,
                'error': {
                    'code': 'INVALID_ARGUMENTS',
                    'message': 'آرگومان‌های ابزار باید شیء باشد.',
                },
            }
            activity['error_code'] = 'INVALID_ARGUMENTS'
            tool_calls.append(activity)
            # حتی فراخوانی نامعتبر هم باید در رد ممیزی ثبت شود.
            audit.record(
                tool_name=tool_name,
                arguments={},
                user=self.user,
                session_id=conversation_id,
                status='failed',
                error_code='INVALID_ARGUMENTS',
                error_message='آرگومان‌های ابزار باید شیء باشد.',
                duration_ms=_elapsed_ms(started),
            )
            return payload

        # دروازهٔ اعتبارسنجی: وجود ابزار، دسترسی کاربر، و اعتبار آرگومان‌ها.
        plan = ToolPlanner(self.registry, self.user).plan(tool_name, arguments)
        if not plan:
            activity['status'] = (
                'denied' if plan.tool else 'failed'
            )
            activity['error_code'] = plan.to_error()['error']['code']
            activity['message'] = plan.reason
            tool_calls.append(activity)
            audit.record(
                tool_name=tool_name,
                arguments=activity['arguments'],
                user=self.user,
                session_id=conversation_id,
                status='denied' if plan.tool else 'failed',
                error_code=activity['error_code'],
                error_message=plan.reason,
                duration_ms=_elapsed_ms(started),
            )
            return plan.to_error()

        tool = plan.tool
        activity['label'] = tool.to_activity_label()
        activity['arguments'] = plan.arguments

        call_started = time.monotonic()
        try:
            result = tool.run(**plan.arguments)
            activity['status'] = 'completed'
        except AIToolError as exc:
            activity['status'] = 'failed'
            activity['error_code'] = exc.code
            activity['message'] = exc.message
            audit.record(
                tool_name=tool.name, arguments=plan.arguments, user=self.user,
                session_id=conversation_id, status='failed',
                error_code=exc.code, error_message=exc.message,
                duration_ms=_elapsed_ms(call_started),
            )
            tool_calls.append(activity)
            return {'success': False, 'error': exc.to_dict()}
        except Exception as exc:
            logger.exception('Unexpected tool failure for %s', tool.name)
            activity['status'] = 'failed'
            activity['error_code'] = 'TOOL_EXECUTION_FAILED'
            activity['message'] = 'اجرای ابزار با خطای غیرمنتظره مواجه شد.'
            audit.record(
                tool_name=tool.name, arguments=plan.arguments, user=self.user,
                session_id=conversation_id, status='failed',
                error_code='TOOL_EXECUTION_FAILED', error_message=type(exc).__name__,
                duration_ms=_elapsed_ms(call_started),
            )
            tool_calls.append(activity)
            return {
                'success': False,
                'error': {
                    'code': 'TOOL_EXECUTION_FAILED',
                    'message': 'اجرای ابزار با خطای غیرمنتظره مواجه شد.',
                    'detail': {'tool': tool.name, 'exception': type(exc).__name__},
                },
            }

        activity['duration_ms'] = _elapsed_ms(call_started)
        tool_calls.append(activity)
        audit.record_result(
            tool.name, plan.arguments, result, user=self.user,
            session_id=conversation_id, duration_ms=activity['duration_ms'],
        )
        return result

    # -- پیام‌ها -----------------------------------------------------
    def _build_messages(self, user_message, context_block, tool_calls):
        messages = [{'role': 'system', 'content': get_system_prompt()}]

        snapshot = context_block.get('factory_snapshot') or {}
        permissions = context_block.get('permissions') or {}
        available_tools = context_block.get('available_tools') or []

        header_lines = [
            '## وضعیت لحظه‌ای CraftFlow (خلاصهٔ تجمیعی، نه جایگزین ابزار)',
            '',
            f'- تاریخ امروز: {context_block.get("current_date", {}).get("today_jalali")}',
            f'- روز کاری: {context_block.get("current_date", {}).get("is_working_day")}',
            f'- کاربر: {context_block.get("current_user", {}).get("display_name")}',
            f'- گروه‌های کاربر: {context_block.get("current_user", {}).get("groups")}',
            f'- ابزارهای در دسترس: {len(available_tools)}',
        ]
        if snapshot.get('production'):
            production = snapshot['production']
            header_lines += [
                f'- سفارش‌ها: {production.get("orders_total")} کل، '
                f'{production.get("orders_active")} باز',
                f'- تسک‌ها: {production.get("tasks_pending")} آماده، '
                f'{production.get("tasks_waiting")} در انتظار، '
                f'{production.get("tasks_completed")} تکمیل‌شده',
            ]
        if snapshot.get('inventory'):
            inventory = snapshot['inventory']
            header_lines += [
                f'- انبار: {inventory.get("total_materials")} ماده، '
                f'{inventory.get("low_stock_count")} کم‌موجود، '
                f'{inventory.get("out_of_stock_count")} ناموجود',
            ]
        header_lines += [
            '',
            'برای هر عدد دقیق یا هر پرسش جزئی، ابزار مربوطه را اجرا کن.',
        ]
        messages.append({'role': 'system', 'content': '\n'.join(header_lines)})

        for item in context_block.get('conversation_history') or []:
            if item.get('role') in ('user', 'assistant') and item.get('content'):
                messages.append({'role': item['role'], 'content': item['content']})

        messages.append({'role': 'user', 'content': user_message})
        return messages

    def describe_tools(self):
        """فهرست ابزارهای مجاز کاربر — برای صفحهٔ شفافیت."""
        return [{
            **tool.to_public_metadata(),
            'label': tool.to_activity_label(),
            'allowed': True,
        } for tool in self.registry.available_for(self.user)]


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)


def chat(user, message, conversation_id=None, history=None, provider=None, enabled=None):
    """نقطهٔ ورود راحت برای ویوها و تست‌ها."""
    return AIOrchestrator(
        user=user, provider=provider, enabled=enabled
    ).chat(message, conversation_id=conversation_id, history=history)