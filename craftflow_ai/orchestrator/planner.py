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

# دلیل رد شدن. این سه نباید خلط شوند: «مدل آرگومان اشتباه ساخته»،
# «کاربر اجازه ندارد» و «چنین ابزاری وجود ندارد» سه رویداد کاملاً متفاوت‌اند
# و در رد ممیزی هم باید متفاوت دیده شوند.
REJECTED_NOT_FOUND = 'not_found'
REJECTED_PERMISSION = 'permission'
REJECTED_ARGUMENTS = 'invalid_arguments'

REJECTION_CODES = {
    REJECTED_NOT_FOUND: 'TOOL_NOT_FOUND',
    REJECTED_PERMISSION: 'PERMISSION_DENIED',
    REJECTED_ARGUMENTS: 'INVALID_ARGUMENTS',
}

# فقط رد به دلیل دسترسی «denied» است؛ بقیه «failed» تا یک اشتباه مدل در
# گزارش امنیتی شبیه تلاش برای دور زدن دسترسی دیده نشود.
REJECTION_STATUSES = {
    REJECTED_NOT_FOUND: 'failed',
    REJECTED_PERMISSION: 'denied',
    REJECTED_ARGUMENTS: 'failed',
}


class ToolCallPlan:
    """نتیجهٔ اعتبارسنجی یک فراخوانی ابزار درخواستی از سمت مدل."""

    REJECTED_NOT_FOUND = REJECTED_NOT_FOUND
    REJECTED_PERMISSION = REJECTED_PERMISSION
    REJECTED_ARGUMENTS = REJECTED_ARGUMENTS
    REJECTION_CODES = REJECTION_CODES
    REJECTION_STATUSES = REJECTION_STATUSES

    def __init__(self, name, arguments, tool=None, allowed=True, reason='',
                 rejection=None, code=None, detail=None):
        self.name = name
        self.arguments = arguments
        self.tool = tool
        self.allowed = allowed
        self.reason = reason
        self.rejection = rejection
        # کد دقیق‌تر از نگاشت عمومی (مثلاً UNKNOWN_ARGUMENT به‌جای
        # INVALID_ARGUMENTS) تا مدل بتواند خطای خودش را اصلاح کند.
        self.code = code
        self.detail = detail or {}

    def __bool__(self):
        return self.allowed

    @property
    def error_code(self):
        if self.allowed:
            return None
        if self.code:
            return self.code
        return self.REJECTION_CODES.get(self.rejection, 'TOOL_ERROR')

    @property
    def status(self):
        """وضعیت فعالیت/ممیزی برای فراخوانی رد شده."""
        if self.allowed:
            return 'completed'
        return self.REJECTION_STATUSES.get(self.rejection, 'failed')

    def to_error(self):
        if self.allowed:
            return {'success': True}
        detail = {'tool': self.name, 'reason': self.rejection}
        detail.update(self.detail)
        return {
            'success': False,
            'error': {
                'code': self.error_code,
                'message': self.reason or 'این ابزار در دسترس نیست.',
                'detail': detail,
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
            return ToolCallPlan(
                name, arguments, tool=None, allowed=False, reason=exc.message,
                rejection=REJECTED_NOT_FOUND,
            )

        if not has_permission(self.user, tool.permission):
            logger.warning('LLM requested tool %s without permission %s', name, tool.permission)
            return ToolCallPlan(
                name, arguments, tool=tool, allowed=False,
                reason=f'کاربر اجازهٔ «{tool.permission}» را ندارد.',
                rejection=REJECTED_PERMISSION,
            )

        try:
            cleaned = tool.validate_arguments(arguments)
        except ToolValidationError as exc:
            # خطای مدل است، نه کاربر: نباید به‌عنوان «رد دسترسی» ثبت شود،
            # چون هم ممیزی را آلوده می‌کند و هم مدل را به توقف زودهنگام
            # واداشته و به کاربر به‌اشتباه می‌گوید دسترسی ندارد.
            logger.info('LLM sent invalid arguments for %s: %s', name, exc.message)
            return ToolCallPlan(
                name, arguments, tool=tool, allowed=False, reason=exc.message,
                rejection=REJECTED_ARGUMENTS,
                code=exc.code, detail=exc.detail,
            )

        return ToolCallPlan(name, cleaned, tool=tool, allowed=True)