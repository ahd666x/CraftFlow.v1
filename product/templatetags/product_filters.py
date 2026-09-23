# product/templatetags/product_filters.py
from django import template
from django.utils import timezone
from django.contrib.humanize.templatetags.humanize import intcomma as humanize_intcomma
import jdatetime


register = template.Library()


@register.filter(name='format_colors')
def format_colors(value):
    """تبدیل دیکشنری رنگ‌ها به لیستی از (نام, کد) برای حلقه‌ی قالب"""
    if not isinstance(value, dict):
        return []
    return value.items()


@register.filter
def split(value, arg):
    return value.split(arg)


@register.filter
def get_item(dictionary, key):
    """دریافت مقدار از دیکشنری با کلید مشخص"""
    if dictionary is None:
        return ''
    return dictionary.get(key, '')


@register.filter
def zip_lists(a, b):
    """ترکیب دو لیست در قالب"""
    return zip(a, b)


@register.filter
def intcomma(value):
    """نمایش اعداد با جداکننده هزارگان"""
    try:
        return humanize_intcomma(int(value))
    except (ValueError, TypeError):
        return value


@register.filter
def multiply(value, arg):
    """ضرب دو عدد"""
    try:
        return float(value) * float(arg)
    except (ValueError, TypeError):
        return 0


@register.filter
def persian_date(value):
    """تبدیل datetime یا date میلادی به رشتهٔ شمسی YYYY/MM/DD"""
    if not value:
        return ''
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value, timezone.get_current_timezone())
        if hasattr(value, 'date'):
            value = value.date()
        return jdatetime.date.fromgregorian(date=value).strftime('%Y/%m/%d')
    except Exception:
        return str(value)


@register.filter
def persian_datetime(value):
    """تبدیل datetime میلادی به رشته‌ی شمسی همراه با ساعت: YYYY/MM/DD HH:MM"""
    if not value:
        return ''
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value, timezone.get_current_timezone())
        d = value.date() if hasattr(value, 'date') else value
        date_str = jdatetime.date.fromgregorian(date=d).strftime('%Y/%m/%d')
        if hasattr(value, 'hour'):
            return f"{date_str} {value.strftime('%H:%M')}"
        return date_str
    except Exception:
        return str(value)


@register.filter
def task_color_code(task):
    """دریافت کد رنگ متناظر با color_part یک وظیفه تولید"""
    if not hasattr(task, 'order_item') or not task.order_item:
        return '-'
    for c in task.order_item.ordercolor.all():
        if c.part == task.color_part:
            return c.code
    return '-'