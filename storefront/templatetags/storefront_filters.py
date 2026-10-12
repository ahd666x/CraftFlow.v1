"""
فیلترهای قالب فروشگاه.

فیلتر عدد فارسی اینجا نگه داشته شده چون قالب‌های فروشگاه نباید به
`product_filters` وابسته باشند؛ آن فیلترها بخشی از پنل تولید‌اند.
"""
from datetime import date, datetime

from django import template

register = template.Library()

PERSIAN_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')


@register.filter
def fa_number(value):
    """ارقام لاتین را به فارسی تبدیل می‌کند."""
    if value is None:
        return ''
    return str(value).translate(PERSIAN_DIGITS)


@register.filter
def fa_price(value):
    """مبلغ را با جداکننده‌ی هزارگان و ارقام فارسی برمی‌گرداند."""
    if value is None:
        return ''
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return str(value).translate(PERSIAN_DIGITS)
    return f'{amount:,}'.translate(PERSIAN_DIGITS)


@register.filter
def persian_date(value, fmt='%Y/%m/%d'):
    """تاریخ میلادی یا شمسی را با قالب شمسی برمی‌گرداند.

    `Order.created_at` از `PersianDateField` است و به‌صورت
    `jdatetime.date` در پایتون ارائه می‌شود؛ میلادی را هم
    برای انعطاف قبول می‌کند."""
    if not value:
        return ''
    if isinstance(value, str):
        return value
    try:
        import jdatetime
        if isinstance(value, jdatetime.date):
            return value.strftime(fmt)
        if isinstance(value, datetime):
            return jdatetime.datetime.fromgregorian(datetime=value).strftime(fmt)
        return jdatetime.date.fromgregorian(date=value).strftime(fmt)
    except (ImportError, TypeError, ValueError, OverflowError):
        return str(value)


@register.filter
def js_number(value):
    """یک مقدار را به صورت رشته‌ی عددی ساده (بدون جداکننده) برمی‌گرداند
    تا بتوان آن را مستقیماً در JavaScript embed کرد."""
    if value is None or value == '':
        return 'null'
    try:
        return str(int(value))
    except (TypeError, ValueError):
        try:
            return str(float(value))
        except (TypeError, ValueError):
            return 'null'
