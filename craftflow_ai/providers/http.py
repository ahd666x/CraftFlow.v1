"""
لایهٔ انتقال HTTP مشترک Providerها.

به‌جای افزودن وابستگی جدید (google-genai / openai / anthropic)، از ``urllib``
کتابخانهٔ استاندارد استفاده می‌شود. این کار عمدی است: افزودن SDKها چند مگابایت
حجم نصب و یک وابستگی خارجی جدید به پروژه تحمیل می‌کرد، در حالی که فقط به
دو متد HTTP (POST JSON با هدر) نیاز داریم.
"""
import json
import logging
import urllib.error
import urllib.request

from craftflow_ai.permissions.errors import (
    AIConfigurationError,
    AITimeoutError,
    AIProviderError,
)

logger = logging.getLogger('craftflow_ai.providers.http')


class HTTPResponseError(AIProviderError):
    pass


def post_json(url, payload, headers, timeout=60):
    """
    یک POST با بدنهٔ JSON.

    خطاها به استثناهای لایهٔ AI تبدیل می‌شوند تا Orchestrator بتواند کد خطای
    یکسان (بدون نشت کلید API) به UI بدهد.
    """
    if not url:
        raise AIConfigurationError(
            'آدرس سرویس LLM تنظیم نشده است.',
            detail={'provider_url_missing': True},
        )

    data = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(url, data=data, method='POST')
    request.add_header('Content-Type', 'application/json')
    for key, value in (headers or {}).items():
        if value:
            request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        # متن خطای سرویس ممکن است اطلاعات حساس داشته باشد؛ فقط کد وضعیت نگه داشته می‌شود.
        body = ''
        try:
            body = exc.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        logger.warning('LLM HTTP error status=%s provider_url=%s', exc.code, url)
        raise HTTPResponseError(
            f'سرویس LLM خطای {exc.code} برگرداند.',
            detail={'status_code': exc.code, 'provider_body': body},
        ) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, 'reason', exc)
        if isinstance(reason, TimeoutError) or 'timed out' in str(reason).lower():
            raise AITimeoutError(
                'پاسخی از سرویس LLM دریافت نشد (اتمام زمان).',
                detail={'timeout_seconds': timeout},
            ) from exc
        raise AIProviderError(
            'ارتباط با سرویس LLM برقرار نشد.',
            detail={'reason': type(exc).__name__},
        ) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise AIProviderError(
            'پاسخ سرویس LLM قابل خواندن نبود.',
            detail={'content_type': 'invalid_json'},
        ) from exc