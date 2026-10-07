from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from cart.services import CartService, get_or_create_cart


def cart_detail(request):
    cart = get_or_create_cart(request)
    return render(request, 'cart/detail.html', {
        'cart': cart,
        'items': cart.items.select_related('product', 'product__category'),
    })


@require_POST
def cart_add(request, product_id):
    """
    افزودن به سبد.

    اگر درخواست HTMX باشد فقط سطر سبد را برمی‌گردانیم (بدون ری‌لود کامل
    صفحه)؛ وگرنه به صفحه‌ی سبد می‌رویم.
    """
    cart = get_or_create_cart(request)

    def _int_or_none(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    try:
        quantity = int(request.POST.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    if quantity < 1:
        quantity = 1

    item = CartService.add_item(
        cart,
        product_id,
        quantity=quantity,
        length=_int_or_none(request.POST.get('length')),
        width=_int_or_none(request.POST.get('width')),
        height=_int_or_none(request.POST.get('height')),
    )

    if request.headers.get('HX-Request'):
        # پاسخ HTMX فقط شماره‌ی سبد است؛ هدف هدر badge است.
        response = render(request, 'cart/_count.html', {'cart': cart})
        response['HX-Trigger'] = 'cartUpdated'
        return response

    messages.success(request, f"«{item.product.name}» به سبد اضافه شد.")
    return redirect('cart:cart_detail')


@require_POST
def cart_remove(request, product_id):
    cart = get_or_create_cart(request)
    CartService.remove_item(cart, product_id)
    messages.success(request, 'محصول از سبد حذف شد.')
    return redirect('cart:cart_detail')


@require_POST
def cart_update(request, product_id):
    cart = get_or_create_cart(request)
    try:
        quantity = int(request.POST.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    CartService.update_quantity(cart, product_id, quantity)
    return redirect('cart:cart_detail')


@require_POST
def cart_clear(request):
    cart = get_or_create_cart(request)
    CartService.clear(cart)
    messages.success(request, 'سبد خرید خالی شد.')
    return redirect('cart:cart_detail')
