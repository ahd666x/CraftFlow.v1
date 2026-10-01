"""خطاهای ساختاریافتهٔ لایهٔ AI.

هر خطا یک ``code`` ثابت دارد تا LLM و UI بتوانند روی رفتار مشخص تکیه کنند،
نه روی متن پیام.
"""


class AIToolError(Exception):
    """پایهٔ خطاهای Tool."""

    code = 'TOOL_ERROR'
    http_status = 400

    def __init__(self, message, code=None, detail=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.detail = detail or {}

    def to_dict(self):
        payload = {'code': self.code, 'message': self.message}
        if self.detail:
            payload['detail'] = self.detail
        return payload


class ToolPermissionError(AIToolError):
    code = 'PERMISSION_DENIED'
    http_status = 403


class ToolNotFoundError(AIToolError):
    code = 'TOOL_NOT_FOUND'
    http_status = 404


class ToolDisabledError(AIToolError):
    """Tool وجود دارد اما در این حالت غیرفعال است (مثلاً عملیات Write در فاز ۱)."""
    code = 'TOOL_DISABLED'
    http_status = 403


class ToolValidationError(AIToolError):
    code = 'INVALID_ARGUMENTS'
    http_status = 400


class ToolExecutionError(AIToolError):
    code = 'TOOL_EXECUTION_FAILED'
    http_status = 500


class AIDisabledError(AIToolError):
    code = 'AI_DISABLED'
    http_status = 503


class AIProviderError(AIToolError):
    code = 'PROVIDER_ERROR'
    http_status = 502


class AITimeoutError(AIProviderError):
    code = 'PROVIDER_TIMEOUT'
    http_status = 504


class AIConfigurationError(AIProviderError):
    code = 'PROVIDER_NOT_CONFIGURED'
    http_status = 503


def http_status_for(code, default=502):
    """
    نگاشت کد خطا به وضعیت HTTP.

    خطاهایی که از مسیر ``OrchestratorResult`` برمی‌گردند exception نیستند، پس
    view برای انتخاب وضعیت به این نگاشت نیاز دارد تا مثلاً «پیکربندی ناقص»
    ۵۰۳ بدهد نه ۵۰۲.
    """
    for klass in (
        AIDisabledError, AIConfigurationError, AITimeoutError, AIProviderError,
        ToolPermissionError, ToolNotFoundError, ToolDisabledError,
        ToolValidationError, ToolExecutionError, AIToolError,
    ):
        if klass.code == code:
            return klass.http_status
    return default