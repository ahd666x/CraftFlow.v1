from datetime import datetime

import jdatetime
from django import template
from django.utils import timezone
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def jalali(value, fmt='%Y/%m/%d %H:%M'):
    if not value:
        return '—'
    if isinstance(value, jdatetime.datetime):
        return value.strftime(fmt)
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return jdatetime.datetime.fromgregorian(datetime=value).strftime(fmt)
    if isinstance(value, jdatetime.date):
        return value.strftime(fmt.split(' ')[0])
    return jdatetime.date.fromgregorian(date=value).strftime(fmt.split(' ')[0])


@register.filter
def encode_url_slashes(value):
    """HTML-encode slashes in trusted reversed URL values for the template attribute."""
    return mark_safe(str(value).replace('/', '&#47;'))
