"""
فیلترهای قالب فروشگاه.

فیلتر عدد فارسی اینجا نگه داشته شده چون قالب‌های فروشگاه نباید به
`product_filters` وابسته باشند؛ آن فیلترها بخشی از پنل تولید‌اند.
"""
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
