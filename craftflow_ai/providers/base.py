"""
قرارداد مشترک همهٔ LLM Providerها.

نکتهٔ مهم معماری: **هیچ Providerای هیچ‌وقت ORM را نمی‌بیند.** Provider فقط
پیام و شمای ابزار می‌گیرد و فقط متن یا فراخوانی ابزار برمی‌گرداند. تمام
اجرای ابزار در Orchestrator انجام می‌شود، نه در Provider.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """درخواست فراخوانی ابزار که مدل تولید کرده است."""
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: Optional[str] = None

    def to_dict(self):
        return {'name': self.name, 'arguments': self.arguments, 'call_id': self.call_id}


@dataclass
class AIResponse:
    """پاسخ نرمال‌شدهٔ یک Provider.

    دقیقاً یکی از این دو حالت رخ می‌دهد:
      * ``tool_calls`` پر است  → مدل می‌خواهد ابزاری اجرا شود
      * ``content`` پر است    → مدل پاسخ نهایی داده است
    """
    content: str = ''
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ''
    raw: Optional[Any] = None
    provider: str = ''
    model: str = ''

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_public_dict(self):
        return {
            'content': self.content,
            'tool_calls': [c.to_dict() for c in self.tool_calls],
            'finish_reason': self.finish_reason,
            'provider': self.provider,
            'model': self.model,
        }


class AIProvider:
    """رابط پایه. برای افزودن Provider جدید فقط همین سه متد لازم است."""

    name = 'base'

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AIResponse:
        """
        messages: فهرست پیام‌ها با قالب ``{'role': ..., 'content': ...}``
        tools:    شمای ابزارها در قالب function-calling استاندارد
        """
        raise NotImplementedError

    def healthcheck(self) -> Dict[str, Any]:
        """بررسی سبک پیکربندی بدون تماس شبکه."""
        return {'provider': self.name, 'configured': True}

    def __repr__(self):
        return f'<{self.__class__.__name__} name={self.name!r}>'