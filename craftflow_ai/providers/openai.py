"""Provider واقعی OpenAI (Chat Completions با function calling)."""
import logging

from craftflow_ai import config
from craftflow_ai.permissions.errors import AIConfigurationError
from craftflow_ai.providers.base import AIProvider, AIResponse, ToolCall
from craftflow_ai.providers.http import post_json

logger = logging.getLogger('craftflow_ai.providers.openai')

DEFAULT_BASE = 'https://api.openai.com/v1'


class OpenAIProvider(AIProvider):
    name = 'openai'

    def __init__(self, api_key=None, model=None, base_url=None, timeout=None):
        self.api_key = api_key if api_key is not None else config.get_api_key()
        self.model = model or config.get_model() or 'gpt-4o-mini'
        self.base_url = (base_url or config.get_base_url() or DEFAULT_BASE).rstrip('/')
        self.timeout = timeout or config.get_timeout()

    def healthcheck(self):
        return {'provider': self.name, 'model': self.model, 'configured': bool(self.api_key)}

    @staticmethod
    def _convert_tools(tools):
        converted = []
        for tool in tools or []:
            converted.append({
                'type': 'function',
                'function': {
                    'name': tool['name'],
                    'description': tool.get('description', ''),
                    'parameters': tool.get('input_schema') or {
                        'type': 'object', 'properties': {}
                    },
                },
            })
        return converted or None

    def chat(self, messages, tools=None, **kwargs):
        if not self.api_key:
            raise AIConfigurationError(
                'کلید API برای OpenAI تنظیم نشده است (CRAFTFLOW_AI_API_KEY).',
                detail={'provider': self.name, 'missing': 'CRAFTFLOW_AI_API_KEY'},
            )

        payload = {
            'model': self.model,
            'messages': [
                m for m in messages
                if m.get('content') or m.get('role') in ('system', 'user', 'assistant')
            ],
            'temperature': kwargs.get('temperature', config.get_temperature()),
        }
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            payload['tools'] = converted_tools
            payload['tool_choice'] = 'auto'

        headers = {'Authorization': f'Bearer {self.api_key}'}
        data = post_json(
            f'{self.base_url}/chat/completions', payload, headers, timeout=self.timeout
        )
        return self._parse(data)

    def _parse(self, data):
        choices = data.get('choices') or []
        if not choices:
            raise AIConfigurationError('سرویس OpenAI پاسخی برنگرداند.', detail={})

        message = choices[0].get('message') or {}
        tool_calls = []
        for call in message.get('tool_calls') or []:
            function = call.get('function') or {}
            tool_calls.append(ToolCall(
                name=function.get('name', ''),
                arguments=function.get('arguments') or {},
                call_id=call.get('id'),
            ))

        return AIResponse(
            content=(message.get('content') or '').strip(),
            tool_calls=tool_calls,
            finish_reason=choices[0].get('finish_reason', ''),
            raw=data,
            provider=self.name,
            model=self.model,
        )