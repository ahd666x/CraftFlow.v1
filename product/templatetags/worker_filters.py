# product/templatetags/worker_filters.py

from django import template

register = template.Library()

@register.filter
def format_costs(costs):
    """تبدیل دیکشنری هزینه‌ها به رشته قابل خواندن"""
    if not costs:
        return ''
    return ', '.join([f"{k}:{v}" for k, v in costs.items() if v > 0])

@register.filter
def div(value, arg):
    """Divide value by arg (value / arg)"""
    try:
        return float(value) / float(arg)
    except (ValueError, ZeroDivisionError, TypeError):
        return 0

@register.filter
def mul(value, arg):
    """Multiply value by arg (value * arg)"""
    try:
        return float(value) * float(arg)
    except (ValueError, TypeError):
        return 0


@register.filter
def sub(value, arg):
    """Subtract arg from value (value - arg)"""
    try:
        return value - arg
    except (TypeError, ValueError):
        return 0
