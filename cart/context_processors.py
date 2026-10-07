from django.urls import resolve


def cart_totals(request):
    """
    تعداد و مبلغ کل سبد را به قالب‌های فروشگاه تزریق می‌کند.

    عمداً فقط برای مسیرهای فروشگاه کار می‌کند. اگر بدون این شرط اجرا شود،
    روی هر درخواستِ پنل/ادمین هم یک سبد می‌سازد و برای هر بازدیدکننده‌ی
    مهمان یک ردیف بی‌استفاده در دیتابیس می‌سازد.
    """
    from cart.services import get_or_create_cart

    empty = {'cart_total_items': 0, 'cart_total_price': 0}

    try:
        match = resolve(request.path_info)
    except Exception:
        return empty

    if not match.url_name or not match.url_name.startswith(('storefront:', 'cart:')):
        return empty

    cart = get_or_create_cart(request)
    return {
        'cart_total_items': cart.total_items,
        'cart_total_price': cart.total_price,
    }
