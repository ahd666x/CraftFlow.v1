"""
برنامه‌ریز اجرای ابزار.

این ماژول عمداً «برنامه‌ریز» است نه «پیش‌بینی‌کننده»: تصمیم نهایی دربارهٔ
اینکه کدام ابزار اجرا شود با LLM است. کار این ماژول فقط دو چیز است:

  1. اعتبارسنجی نهایی فراخوانی مدل پیش از هرگونه اجرا.
  2. نگاشت فراخوانی به شمای استاندارد و ثبت فعالیت برای UI.

اگر در فاز ۲ مسیرهای چندمرحله‌ای (مثلاً «سفارش را وارد تولید کن») اضافه شود،
اینجا محل طبیعی آن است — نه داخل Tool و نه داخل Prompt.
"""
import logging

from craftflow_ai.permissions.errors import ToolNotFoundError, ToolValidationError
from craftflow_ai.permissions.policy import has_permission

logger = logging.getLogger('craftflow_ai.orchestrator.planner')


class ToolCallPlan:
    """نتیجهٔ اعتبارسنجی یک فراخوانی ابزار درخواستی از سمت مدل."""

    def __init__(self, name, arguments, tool=None, allowed=True, reason=''):
        self.name = name
        self.arguments = arguments
        self.tool = tool
        self.allowed = allowed
        self.reason = reason

    def __bool__(self):
        return self.allowed

    def to_error(self):
        return {
            'success': False,
            'error': {
                'code': 'PERMISSION_DENIED' if self.tool else 'TOOL_NOT_FOUND',
                'message': self.reason or 'این ابزار در دسترس نیست.',
                'detail': {'tool': self.name},
            },
        }


class ToolPlanner:
    """دروازهٔ اعتبارسنجی پیش از اجرای ابزار."""

    def __init__(self, registry, user):
        self.registry = registry
        self.user = user

    def plan(self, name, arguments):
        try:
            tool = self.registry.get(name)
        except ToolNotFoundError as exc:
            logger.warning('LLM requested unknown tool: %s', name)
            return ToolCallPlan(name, arguments, tool=None, allowed=False, reason=exc.message)

        if not has_permission(self.user, tool.permission):
            logger.warning('LLM requested tool %s without permission %s', name, tool.permission)
            return ToolCallPlan(
                name, arguments, tool=tool, allowed=False,
                reason=f'کاربر اجازهٔ «{tool.permission}» را ندارد.',
            )

        try:
            cleaned = tool.validate_arguments(arguments)
        except ToolValidationError as exc:
            return ToolCallPlan(
                name, arguments, tool=tool, allowed=False, reason=exc.message
            )

        return ToolCallPlan(name, cleaned, tool=tool, allowed=True)