"""
ویوهای لایهٔ AI.

دسترسی: صفحهٔ گفتگو فقط برای کاربران واردشده (سازگار با بقیهٔ پروژه که از
``login_required`` و دکوراتورهای گروهی استفاده می‌کند). دسترسی به *داده* در
لایهٔ Tool و با همان گروه‌های CraftFlow کنترل می‌شود.
"""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from craftflow_ai import config
from craftflow_ai.models import AIConversation, AIConversationMessage
from craftflow_ai.orchestrator.manager import AIOrchestrator
from craftflow_ai.permissions.errors import AIDisabledError, AIToolError, http_status_for
from craftflow_ai.permissions.policy import permissions_for_user
from craftflow_ai.tools import get_registry

logger = logging.getLogger('craftflow_ai.views')

MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_MESSAGES = 20


def _parse_body(request):
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body or b'{}'), None
        except (ValueError, TypeError):
            return None, 'بدنهٔ درخواست JSON معتبر نیست.'
    if request.body:
        return {'message': request.POST.get('message', '')}, None
    return {'message': ''}, None


def _get_or_create_conversation(user, conversation_id):
    if conversation_id:
        conversation = (
            AIConversation.objects
            .filter(conversation_id=conversation_id, user=user)
            .first()
        )
        if conversation:
            return conversation, False
    conversation = AIConversation.objects.create(
        conversation_id=conversation_id or _new_id(),
        user=user,
    )
    return conversation, True


def _new_id():
    import uuid
    return uuid.uuid4().hex


def _history_for(conversation, limit=MAX_HISTORY_MESSAGES):
    messages = list(
        conversation.messages.order_by('-created_at')[:limit]
    )
    return [
        {'role': m.role, 'content': m.content} for m in reversed(messages)
    ]


@login_required
def chat_view(request):
    """صفحهٔ گفتگو با دستیار هوشمند CraftFlow."""
    registry = get_registry()
    allowed = registry.available_for(request.user)
    return render(request, 'craftflow_ai/chat.html', {
        'ai_enabled': config.is_enabled(),
        'ai_config': config.public_config(),
        'tools': [{
            'name': tool.name,
            'label': tool.to_activity_label(),
            'description': tool.description,
            'permission': tool.permission,
            'category': tool.category,
        } for tool in allowed],
        'tool_count': len(allowed),
        'permissions': permissions_for_user(request.user),
        'active_nav': 'ai',
    })


@login_required
@require_POST
def chat_api(request):
    """
    endpoint گفتگو.

    Request : {"message": "...", "conversation_id": "..."}
    Response: {"success": true, "message": "...", "conversation_id": "...",
               "tool_calls": [{"name": ..., "label": ..., "status": ...}]}
    """
    payload, parse_error = _parse_body(request)
    if parse_error:
        return JsonResponse(
            {'success': False, 'error': {'code': 'INVALID_JSON', 'message': parse_error}},
            status=400,
        )

    message = (payload.get('message') or '').strip()
    if not message:
        return JsonResponse(
            {'success': False, 'error': {
                'code': 'EMPTY_MESSAGE', 'message': 'پیام خالی است.'
            }},
            status=400,
        )
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS]

    conversation_id = (payload.get('conversation_id') or '').strip() or None

    if not config.is_enabled():
        return JsonResponse(
            {'success': False, 'error': {
                'code': 'AI_DISABLED',
                'message': 'دستیار هوشمند CraftFlow غیرفعال است.',
            }},
            status=503,
        )

    conversation, _created = _get_or_create_conversation(request.user, conversation_id)
    history = _history_for(conversation)

    try:
        result = AIOrchestrator(request.user).chat(
            message=message,
            conversation_id=conversation.conversation_id,
            history=history,
        )
    except AIDisabledError as exc:
        return JsonResponse({'success': False, 'error': exc.to_dict()}, status=exc.http_status)
    except AIToolError as exc:
        return JsonResponse({'success': False, 'error': exc.to_dict()}, status=exc.http_status)
    except Exception:
        logger.exception('AI chat failed for conversation=%s', conversation.conversation_id)
        return JsonResponse(
            {'success': False, 'error': {
                'code': 'INTERNAL_ERROR',
                'message': 'خطای غیرمنتظره در پردازش پیام رخ داد.',
            }},
            status=500,
        )

    conversation.touch(provider=result.provider, model=result.model)
    if not result.error:
        AIConversationMessage.objects.create(
            conversation=conversation, role='user', content=message,
        )
        AIConversationMessage.objects.create(
            conversation=conversation, role='assistant', content=result.answer,
        )

    return JsonResponse(
        result.to_dict(),
        status=200 if result.success else http_status_for(result.error_code),
    )


@login_required
def chat_history(request, conversation_id):
    """تاریخچهٔ یک گفتگوی مشخص (فقط گفتگوی خود کاربر)."""
    conversation = (
        AIConversation.objects
        .filter(conversation_id=conversation_id, user=request.user)
        .first()
    )
    if conversation is None:
        return JsonResponse(
            {'success': False, 'error': {
                'code': 'CONVERSATION_NOT_FOUND', 'message': 'گفتگو یافت نشد.'
            }},
            status=404,
        )
    return JsonResponse({
        'success': True,
        'conversation_id': conversation.conversation_id,
        'messages': [{
            'role': m.role,
            'content': m.content,
            'created_at': m.created_at.isoformat(),
        } for m in conversation.messages.all()],
    })


@login_required
def tools_api(request):
    """فهرست ابزارهای در دسترس کاربر — برای شفافیت (نه جزئیات حساس)."""
    registry = get_registry()
    return JsonResponse({
        'success': True,
        'tools': [{
            'name': tool.name,
            'label': tool.to_activity_label(),
            'description': tool.description,
            'permission': tool.permission,
            'read_only': tool.read_only,
            'category': tool.category,
        } for tool in registry.available_for(request.user)],
        'permissions': permissions_for_user(request.user),
        'enabled': config.is_enabled(),
    })