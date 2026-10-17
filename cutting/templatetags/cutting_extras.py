from datetime import datetime

import jdatetime
from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def jalali(value, fmt='%Y/%m/%d %H:%M'):
    if not value:
        return '—'
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return jdatetime.datetime.fromgregorian(datetime=value).strftime(fmt)
    return jdatetime.date.fromgregorian(date=value).strftime(fmt.split(' ')[0])
