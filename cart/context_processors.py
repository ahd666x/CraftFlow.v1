from django.urls import resolve


def cart_totals(request):
    """
    تعداد و مبلغ کل سبد را به قالب‌های فروشگاه تزریق می‌کند.

    عمداً فقط برای مسیرهای فروشگاه کار می‌کند. اگر بدون این شرط اجرا شود،
    روی هر درخواستِ پنل/ادمین هم یک سبد می‌سازد و برای هر بازدیدکننده‌ی
    مهمان یک ردیف بی‌استفاده در دیتابیس می‌سازد.
    """
    from cart.models import Cart

    empty = {'cart_total_items': 0, 'cart_total_price': 0}

    try:
        match = resolve(request.path_info)
    except Exception:
        return empty

    STORE_NAMESPACES = {'storefront', 'cart', 'accounts', 'payments', 'discounts'}
    if match.namespace not in STORE_NAMESPACES:
        return empty

    if request.user.is_authenticated:
        cart = Cart.objects.filter(user=request.user).first()
    else:
        key = request.session.session_key
        cart = Cart.objects.filter(session_key=key).first() if key else None
    if cart is None:
        return empty

    items = list(cart.items.all())
    return {
        'cart_total_items': sum(i.quantity for i in items),
        'cart_total_price': sum(i.total_price for i in items),
    }
