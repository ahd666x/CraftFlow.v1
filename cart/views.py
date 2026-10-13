from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from cart.services import CartService, get_or_create_cart


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


@require_POST
def cart_add(request, product_id):
    """
    افزودن به سبد.

    اگر درخواست HTMX باشد فقط سطر سبد را برمی‌گردانیم (بدون ری‌لود کامل
    صفحه)؛ وگرنه به صفحه‌ی سبد می‌رویم.
    """
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

    try:
        item = CartService.add_item(
            cart,
            product_id,
            quantity=quantity,
            length=_int_or_none(request.POST.get('length')),
            width=_int_or_none(request.POST.get('width')),
            height=_int_or_none(request.POST.get('height')),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        if request.headers.get('HX-Request'):
            # برای HTMX یک پاسخ 422 برمی‌گردانیم تا swap انجام نشود
            from django.http import HttpResponse
            return HttpResponse(str(exc), status=422)
        return redirect(request.META.get('HTTP_REFERER') or 'cart:cart_detail')

    if request.headers.get('HX-Request'):
        # پاسخ HTMX فقط شماره‌ی سبد است؛ هدف هدر badge است.
        response = render(request, 'cart/_count.html', {'cart': cart})
        response['HX-Trigger'] = '{"cartUpdated": {"count": ' + str(cart.total_items) + '}}'
        return response

    messages.success(request, f"«{item.product.name}» به سبد اضافه شد.")
    return redirect('cart:cart_detail')


@require_POST
def cart_remove(request, item_id):
    cart = get_or_create_cart(request)
    CartService.remove_item(cart, item_id)
    messages.success(request, 'محصول از سبد حذف شد.')
    
    if request.headers.get('HX-Request'):
        items = cart.items.select_related('product', 'product__category').prefetch_related('product__gallery_images')
        response = render(request, 'cart/_mini_cart.html', {'cart': cart, 'items': items})
        response['HX-Trigger'] = '{"cartUpdated": {"count": ' + str(cart.total_items) + '}}'
        return response
    
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
        items = cart.items.select_related('product', 'product__category').prefetch_related('product__gallery_images')
        response = render(request, 'cart/_mini_cart.html', {'cart': cart, 'items': items})
        response['HX-Trigger'] = '{"cartUpdated": {"count": ' + str(cart.total_items) + '}}'
        return response
    
    return redirect('cart:cart_detail')


@require_POST
def cart_clear(request):
    cart = get_or_create_cart(request)
    CartService.clear(cart)
    messages.success(request, 'سبد خرید خالی شد.')
    
    if request.headers.get('HX-Request'):
        items = cart.items.select_related('product', 'product__category').prefetch_related('product__gallery_images')
        response = render(request, 'cart/_mini_cart.html', {'cart': cart, 'items': items})
        response['HX-Trigger'] = '{"cartUpdated": {"count": ' + str(cart.total_items) + '}}'
        return response
    
    return redirect('cart:cart_detail')
