"""Provider واقعی Gemini (Google Generative Language API) با tool calling."""
import logging

from craftflow_ai import config
from craftflow_ai.permissions.errors import AIConfigurationError
from craftflow_ai.providers.base import AIProvider, AIResponse, ToolCall
from craftflow_ai.providers.http import post_json

logger = logging.getLogger('craftflow_ai.providers.gemini')

DEFAULT_BASE = 'https://generativelanguage.googleapis.com/v1beta'

# کلیدهایی که Gemini در شمای ورودی ابزار نمی‌پذیرد و باید پیش از ارسال حذف شوند.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({'additionalProperties'})


class GeminiProvider(AIProvider):
    name = 'gemini'

    def __init__(self, api_key=None, model=None, base_url=None, timeout=None):
        self.api_key = api_key if api_key is not None else config.get_api_key()
        self.model = model or config.get_model() or 'gemini-2.5-flash'
        self.base_url = (base_url or config.get_base_url() or DEFAULT_BASE).rstrip('/')
        self.timeout = timeout or config.get_timeout()

    # -- پیکربندی ---------------------------------------------------
    def _require_key(self):
        if not self.api_key:
            raise AIConfigurationError(
                'کلید API برای Gemini تنظیم نشده است (CRAFTFLOW_AI_API_KEY).',
                detail={'provider': self.name, 'missing': 'CRAFTFLOW_AI_API_KEY'},
            )

    def _endpoint(self):
        model = self.model if self.model.startswith('models/') else f'models/{self.model}'
        return f'{self.base_url}/{model}:generateContent'

    def healthcheck(self):
        return {'provider': self.name, 'model': self.model, 'configured': bool(self.api_key)}

    # -- تبدیل پیام -------------------------------------------------
    def _convert_messages(self, messages):
        """Gemini قالب system را جدا از contents می‌خواهد."""
        system_parts, contents = [], []
        for message in messages:
            role = message.get('role', 'user')
            content = message.get('content') or ''
            if not content:
                continue
            if role == 'system':
                system_parts.append({'text': content})
            elif role == 'assistant':
                contents.append({'role': 'model', 'parts': [{'text': content}]})
            elif role == 'tool':
                contents.append({
                    'role': 'user',
                    'parts': [{'text': content}],
                })
            else:
                contents.append({'role': 'user', 'parts': [{'text': content}]})
        if not contents:
            contents = [{'role': 'user', 'parts': [{'text': 'سلام'}]}]
        return system_parts, contents

    @staticmethod
    def _sanitize_schema(node):
        """
        Gemini زیرمجموعهٔ محدودی از JSON Schema را می‌پذیرد و هر کلید ناشناخته
        را با خطای ۴۰۰ رد می‌کند (``Unknown name ... at parameters``).

        ``additionalProperties`` در Gemini پشتیبانی نمی‌شود، اما رجیستری
        CraftFlow برای بستنِ آرگومان‌های ناشناخته به آن نیاز دارد
        (``Tool.validate_arguments``). بنابراین حذفش در این مرز انجام می‌شود،
        نه در schema مشترکی که OpenAI/Anthropic هم از آن استفاده می‌کنند.
        """
        if isinstance(node, dict):
            return {
                key: GeminiProvider._sanitize_schema(value)
                for key, value in node.items()
                if key not in _UNSUPPORTED_SCHEMA_KEYS
            }
        if isinstance(node, list):
            return [GeminiProvider._sanitize_schema(item) for item in node]
        return node

    @staticmethod
    def _convert_tools(tools):
        declarations = []
        for tool in tools or []:
            schema = tool.get('input_schema') or {
                'type': 'object', 'properties': {}
            }
            declarations.append({
                'name': tool['name'],
                'description': tool.get('description', ''),
                'parameters': GeminiProvider._sanitize_schema(schema),
            })
        return [{'functionDeclarations': declarations}] if declarations else None

    # -- API --------------------------------------------------------
    def chat(self, messages, tools=None, **kwargs):
        self._require_key()
        system_parts, contents = self._convert_messages(messages)

        payload = {
            'contents': contents,
            'generationConfig': {
                'temperature': kwargs.get('temperature', config.get_temperature()),
            },
        }
        if system_parts:
            payload['systemInstruction'] = {'parts': system_parts}
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            payload['tools'] = converted_tools

        url = self._endpoint()
        headers = {'x-goog-api-key': self.api_key}
        data = post_json(url, payload, headers, timeout=self.timeout)
        return self._parse(data)

    def _parse(self, data):
        candidates = data.get('candidates') or []
        if not candidates:
            feedback = (data.get('promptFeedback') or {}).get('blockReason')
            raise AIConfigurationError(
                'سرویس Gemini پاسخی برنگرداند.',
                detail={'block_reason': feedback} if feedback else {},
            )

        candidate = candidates[0]
        parts = (candidate.get('content') or {}).get('parts') or []
        texts, tool_calls = [], []
        for index, part in enumerate(parts):
            function_call = part.get('functionCall')
            if function_call:
                tool_calls.append(ToolCall(
                    name=function_call.get('name', ''),
                    arguments=function_call.get('args') or {},
                    call_id=str(function_call.get('id') or f'call_{index}'),
                ))
            elif part.get('text'):
                texts.append(part['text'])

        return AIResponse(
            content=''.join(texts).strip(),
            tool_calls=tool_calls,
            finish_reason=candidate.get('finishReason', ''),
            raw=data,
            provider=self.name,
            model=self.model,
        )