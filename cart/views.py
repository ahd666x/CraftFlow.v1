import json

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render, reverse
from django.views.decorators.http import require_POST

from cart.services import CartService, get_or_create_cart
from storefront.permissions import can_see_prices


def cart_detail(request):
    cart = get_or_create_cart(request)
    return render(request, 'cart/detail.html', {
        'cart': cart,
        'items': cart.items.select_related(
            'product', 'product__category',
        ).prefetch_related('product__gallery_images'),
    })


def mini_cart(request):
    """Partial view for mini-cart drawer (HTMX)."""
    cart = get_or_create_cart(request)
    items = cart.items.select_related(
        'product', 'product__category',
    ).prefetch_related('product__gallery_images')
    return render(request, 'cart/_mini_cart.html', {
        'cart': cart,
        'items': items,
    })


def _hx_items(cart):
    return cart.items.select_related(
        'product', 'product__category',
    ).prefetch_related('product__gallery_images')


def _hx_response(request, cart, message=None, silent=True):
    """پاسخ جزئی HTMX بر اساس HX-Target.

    * هدف #cart-rows  → پارشیال صفحهٔ سبد (سطرها + OOB جمع‌ها)
    * بقیه            → مینی‌کارت
    اگر سبد خالی شده باشد، کل صفحه ری‌لود می‌شود تا حالت خالی رندر شود.

    پیام کاربر با HX-Trigger (cartMessage) به toast می‌رود؛ از
    messages.* استفاده نمی‌کنیم تا سشن با پاسخ‌های جزئی آلوده نشود و
    پیام‌ها روی صفحه‌های بعدی ظاهر نشوند.
    """
    items = _hx_items(cart)
    if not items.exists():
        response = HttpResponse(status=204)
        response['HX-Redirect'] = reverse('cart:cart_detail')
        return response

    if request.headers.get('HX-Target') == 'cart-rows':
        response = render(request, 'cart/_cart_page.html', {'cart': cart, 'items': items})
    else:
        response = render(request, 'cart/_mini_cart.html', {'cart': cart, 'items': items})

    trigger = {'cartUpdated': {'count': cart.total_items}}
    if silent:
        trigger['cartUpdated']['silent'] = True
    if message:
        trigger['cartMessage'] = message
    response['HX-Trigger'] = json.dumps(trigger, ensure_ascii=False)
    return response


@require_POST
def cart_add(request, product_id):
    """
    افزودن به سبد.

    اگر درخواست HTMX باشد فقط شمارندهٔ سبد را برمی‌گردانیم (بدون ری‌لود کامل
    صفحه)؛ وگرنه به صفحه‌ی سبد می‌رویم.
    """
    if not can_see_prices(request.user):
        # مهمان: به صفحهٔ ورود هدایت می‌شود (با next) تا بن‌بست «تماس بگیرید»
        # برای کسی که فقط هنوز وارد نشده تجربهٔ گمراه‌کننده‌ای نباشد.
        # کاربر لاگینِ بی‌مجوز: پیام واقعی + برگشت به همان صفحه.
        from django.contrib.auth.views import redirect_to_login
        referer = request.META.get('HTTP_REFERER')
        if request.user.is_authenticated:
            messages.info(
                request,
                'ثبت سفارش آنلاین فقط برای حساب‌های تأییدشده فعال است؛ برای ثبت سفارش با ما تماس بگیرید.',
            )
            return redirect(referer or 'storefront:product_list')
        messages.info(request, 'برای ثبت سفارش ابتدا وارد حساب کاربری شوید.')
        return redirect_to_login(referer or reverse('storefront:product_list'))

    cart = get_or_create_cart(request)

    def _int_or_none(value):
        """Parse integer value, returning None only for empty/invalid input.

        Returns 0 for explicit '0' input to allow validation to catch it
        as an out-of-range value rather than silently falling back to default.
        """
        if value is None or value == '':
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    try:
        quantity = int(request.POST.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    if quantity < 1:
        quantity = 1

    # کدهای رنگ انتخاب‌شده برای بخش‌های رنگی؛ فیلدها با پیشوند color_ ارسال
    # می‌شوند (مثل color_بدنه=9). مقدار خالی یعنی همان پیش‌فرض محصول.
    colors = {}
    for key in request.POST:
        if key.startswith('color_') and len(key) > 6:
            code = (request.POST.get(key) or '').strip()
            if code:
                colors[key[6:]] = code

    try:
        item = CartService.add_item(
            cart,
            product_id,
            quantity=quantity,
            length=_int_or_none(request.POST.get('length')),
            width=_int_or_none(request.POST.get('width')),
            height=_int_or_none(request.POST.get('height')),
            colors=colors,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        if request.headers.get('HX-Request'):
            # ۲۰۴: هیچ محتوایی در هدف swap نمی‌شود؛ پیام خطا از طریق
            # هدر HX-Trigger و رویداد cartError به صورت toast نمایش داده می‌شود
            response = HttpResponse(status=204)
            response['HX-Trigger'] = json.dumps({'cartError': str(exc)}, ensure_ascii=False)
            return response
        return redirect(request.META.get('HTTP_REFERER') or 'cart:cart_detail')

    if request.headers.get('HX-Request'):
        # پاسخ HTMX فقط شماره‌ی سبد است؛ هدف هدر badge است.
        response = render(request, 'cart/_count.html', {'cart': cart})
        response['HX-Trigger'] = json.dumps(
            {'cartUpdated': {'count': cart.total_items}}, ensure_ascii=False,
        )
        return response

    messages.success(request, f"«{item.product.name}» به سبد اضافه شد.")
    return redirect('cart:cart_detail')


@require_POST
def cart_remove(request, item_id):
    cart = get_or_create_cart(request)
    CartService.remove_item(cart, item_id)

    if request.headers.get('HX-Request'):
        return _hx_response(request, cart, message='محصول از سبد حذف شد.')

    messages.success(request, 'محصول از سبد حذف شد.')
    return redirect('cart:cart_detail')


@require_POST
def cart_update(request, item_id):
    cart = get_or_create_cart(request)
    try:
        quantity = int(request.POST.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    CartService.update_quantity(cart, item_id, quantity)

    if request.headers.get('HX-Request'):
        return _hx_response(request, cart)

    return redirect('cart:cart_detail')


@require_POST
def cart_clear(request):
    cart = get_or_create_cart(request)
    CartService.clear(cart)

    if request.headers.get('HX-Request'):
        return _hx_response(request, cart, message='سبد خرید خالی شد.')

    messages.success(request, 'سبد خرید خالی شد.')
    return redirect('cart:cart_detail')
