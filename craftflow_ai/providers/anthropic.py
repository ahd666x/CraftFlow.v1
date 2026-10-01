"""Provider واقعی Anthropic (Messages API با tool use)."""
import logging

from craftflow_ai import config
from craftflow_ai.permissions.errors import AIConfigurationError
from craftflow_ai.providers.base import AIProvider, AIResponse, ToolCall
from craftflow_ai.providers.http import post_json

logger = logging.getLogger('craftflow_ai.providers.anthropic')

DEFAULT_BASE = 'https://api.anthropic.com/v1'
ANTHROPIC_VERSION = '2023-06-01'


class AnthropicProvider(AIProvider):
    name = 'anthropic'

    def __init__(self, api_key=None, model=None, base_url=None, timeout=None):
        self.api_key = api_key if api_key is not None else config.get_api_key()
        self.model = model or config.get_model() or 'claude-3-5-haiku-latest'
        self.base_url = (base_url or config.get_base_url() or DEFAULT_BASE).rstrip('/')
        self.timeout = timeout or config.get_timeout()

    def healthcheck(self):
        return {'provider': self.name, 'model': self.model, 'configured': bool(self.api_key)}

    @staticmethod
    def _convert_tools(tools):
        converted = []
        for tool in tools or []:
            converted.append({
                'name': tool['name'],
                'description': tool.get('description', ''),
                'input_schema': tool.get('input_schema') or {
                    'type': 'object', 'properties': {}
                },
            })
        return converted or None

    def chat(self, messages, tools=None, **kwargs):
        if not self.api_key:
            raise AIConfigurationError(
                'کلید API برای Anthropic تنظیم نشده است (CRAFTFLOW_AI_API_KEY).',
                detail={'provider': self.name, 'missing': 'CRAFTFLOW_AI_API_KEY'},
            )

        system_prompt, chat_messages = [], []
        for message in messages:
            role = message.get('role', 'user')
            content = message.get('content') or ''
            if role == 'system':
                system_prompt.append(content)
            elif role in ('user', 'assistant'):
                chat_messages.append({'role': role, 'content': content})
        if not chat_messages:
            chat_messages = [{'role': 'user', 'content': 'سلام'}]

        payload = {
            'model': self.model,
            'max_tokens': 2048,
            'messages': chat_messages,
            'temperature': kwargs.get('temperature', config.get_temperature()),
        }
        if system_prompt:
            payload['system'] = '\n\n'.join(system_prompt)
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            payload['tools'] = converted_tools

        headers = {
            'x-api-key': self.api_key,
            'anthropic-version': ANTHROPIC_VERSION,
        }
        data = post_json(f'{self.base_url}/messages', payload, headers, timeout=self.timeout)
        return self._parse(data)

    def _parse(self, data):
        blocks = data.get('content') or []
        if not blocks:
            raise AIConfigurationError('سرویس Anthropic پاسخی برنگرداند.', detail={})

        texts, tool_calls = [], []
        for index, block in enumerate(blocks):
            if block.get('type') == 'text':
                texts.append(block.get('text') or '')
            elif block.get('type') == 'tool_use':
                tool_calls.append(ToolCall(
                    name=block.get('name', ''),
                    arguments=block.get('input') or {},
                    call_id=block.get('id') or f'call_{index}',
                ))

        return AIResponse(
            content=''.join(texts).strip(),
            tool_calls=tool_calls,
            finish_reason=data.get('stop_reason', ''),
            raw=data,
            provider=self.name,
            model=self.model,
        )