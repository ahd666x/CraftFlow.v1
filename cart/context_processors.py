from django.urls import resolve

from storefront.permissions import can_see_prices


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

    # values_list: بدون ساخت مدل کامل آیتم‌ها؛ unit_price فیلد لوکال آیتم است
    # (Cart.total_price هم همین فرمول را دارد) و مبلغ کل را فقط برای
    # کاربرِ دارای مجوز قیمت محاسبه می‌کنیم.
    total_items = 0
    total_price = 0
    for quantity, unit_price in cart.items.values_list('quantity', 'unit_price'):
        total_items += quantity
        if can_see_prices(request.user):
            total_price += (unit_price or 0) * quantity
    return {
        'cart_total_items': total_items,
        'cart_total_price': total_price,
    }
