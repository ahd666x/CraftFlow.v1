from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from cart.services import get_or_create_cart
from product.models import Color, Customer, Order, OrderItem, Product, ProductCategory
from storefront.models import Address


def _with_rating(queryset):
    """میانگین و تعداد نظرات را یک‌بار annotate می‌کند تا در لیست N+1 نداشته باشیم."""
    return queryset.annotate(
        avg_rating=Avg('reviews__rating', filter=Q(reviews__is_active=True)),
        rev_count=Count('reviews', filter=Q(reviews__is_active=True)),
    )


def _sort_queryset(queryset, sort):
    """مرتب‌سازی یک‌دست برای همه صفحات محصولات."""
    if sort == 'price_asc':
        return queryset.order_by('base_price')
    if sort == 'price_desc':
        return queryset.order_by('-base_price')
    if sort == 'name':
        return queryset.order_by('name')
    return queryset.order_by('-id')


def home(request):
    products = _with_rating(
        Product.active_objects.select_related('category')
        .prefetch_related('gallery_images')
        .order_by('-id')[:12]
    )
    categories = ProductCategory.objects.filter(is_active=True).annotate(
        product_count=Count('products', filter=Q(products__is_active=True)),
    )
    return render(request, 'storefront/home.html', {
        'products': products,
        'categories': categories,
    })


def product_list(request):
    queryset = Product.active_objects.select_related('category').prefetch_related('gallery_images')

    q = request.GET.get('q', '').strip()
    if q:
        queryset = queryset.filter(
            Q(name__icontains=q) | Q(description__icontains=q)
        )

    category_slug = request.GET.get('category', '').strip()
    if category_slug:
        queryset = queryset.filter(category__slug=category_slug)

    sort = request.GET.get('sort', '')
    queryset = _sort_queryset(queryset, sort)

    return render(request, 'storefront/product_list.html', {
        'products': _with_rating(queryset),
        'categories': ProductCategory.objects.filter(is_active=True),
        'q': q,
        'selected_category': category_slug,
        'sort': sort,
    })


def product_detail(request, slug):
    product = get_object_or_404(
        _with_rating(Product.active_objects.select_related('category')),
        slug=slug,
    )
    gallery = product.gallery_images.filter(is_active=True)
    related_products = _with_rating(
        Product.active_objects
        .filter(category=product.category)
        .exclude(pk=product.pk)
        .select_related('category')
        .prefetch_related('gallery_images')
        .order_by('-id')[:6]
    )
    reviews = product.reviews.filter(is_active=True).select_related('user')

    context = {
        'product': product,
        'gallery': gallery,
        'default_colors': product.default_colors or {},
        'related_products': related_products,
        'reviews': reviews,
    }
    return render(request, 'storefront/product_detail.html', context)


def category_list(request):
    categories = ProductCategory.objects.filter(is_active=True).annotate(
        product_count=Count('products', filter=Q(products__is_active=True)),
    )
    return render(request, 'storefront/category_list.html', {'categories': categories})


def category_detail(request, slug):
    category = get_object_or_404(ProductCategory, slug=slug, is_active=True)
    queryset = Product.active_objects.filter(category=category).select_related('category').prefetch_related('gallery_images')
    q = request.GET.get('q', '').strip()
    if q:
        queryset = queryset.filter(Q(name__icontains=q) | Q(description__icontains=q))
    sort = request.GET.get('sort', '')
    queryset = _sort_queryset(queryset, sort)
    return render(request, 'storefront/product_list.html', {
        'products': _with_rating(queryset),
        'categories': ProductCategory.objects.filter(is_active=True),
        'selected_category': slug,
        'current_category': category,
        'q': q,
        'sort': sort,
    })


# ---------------------------------------------------------------- checkout

@login_required
def checkout(request):
    cart = get_or_create_cart(request)
    items = list(cart.items.select_related('product', 'product__category'))
    if not items:
        messages.warning(request, 'سبد خرید شما خالی است.')
        return redirect('cart:cart_detail')

    if request.method == 'POST':
        return _place_order(request, cart, items)

    customer = Customer.objects.filter(user=request.user).first()
    addresses = Address.objects.filter(user=request.user, is_active=True)
    return render(request, 'storefront/checkout.html', {
        'cart': cart,
        'items': items,
        'customer': customer,
        'addresses': addresses,
        # متن آدرس‌ها برای پر کردن خودکار textarea در تسویه
        # (json_script در قالب آن را به‌صورت امن تولید می‌کند)
        'addresses_data': {str(address.pk): address.address for address in addresses},
    })


def _place_order(request, cart, items):
    """ثبت نهایی سفارش. کل مسیر داخل یک تراکنش است."""
    with transaction.atomic():
        return _create_order(request, cart, items)


def _create_order(request, cart, items):
    shipping_address = (request.POST.get('shipping_address') or '').strip()
    if not shipping_address:
        messages.error(request, 'آدرس ارسال را وارد کنید.')
        return redirect('storefront:checkout')

    notes = (request.POST.get('notes') or '').strip()
    phone = (request.POST.get('phone') or '').strip()
    name = (request.POST.get('name') or '').strip() or request.user.get_full_name() or request.user.username

    address = None
    address_id = request.POST.get('address_id')
    if address_id:
        address = Address.objects.filter(
            pk=address_id, user=request.user, is_active=True
        ).first()

    customer = Customer.objects.filter(user=request.user).first()
    if not customer:
        customer = Customer.objects.create(
            user=request.user, name=name, phone=phone, address=shipping_address,
        )
    elif phone and customer.phone != phone:
        customer.phone = phone
        customer.save(update_fields=['phone'])

    order = Order.objects.create(
        user=request.user,
        customer=customer,
        status='draft',
        shipping_address=shipping_address,
        address=address,
    )

    total = 0
    for item in items:
        product = item.product
        # رشته‌ی `size` را هم پر می‌کنیم: `Order.generate_tasks()` از آن برای
        # اعمال اختلاف ابعاد سفارشی به قطعات BOM استفاده می‌کند؛ بدون آن
        # تسک‌ها با ابعاد پیش‌فرض ساخته می‌شدند نه ابعاد مشتری.
        size_parts = [
            str(v) for v in (item.custom_length, item.custom_width, item.custom_height) if v
        ]
        order_item = OrderItem(
            order=order,
            product=product,
            quantity=item.quantity,
            notes='',
            size='x'.join(size_parts),
            length=item.custom_length,
            width=item.custom_width,
            height=item.custom_height,
            # قیمتی که در سبد (با ابعاد سفارشی) محاسبه شده است.
            unit_price=item.unit_price,
        )
        # save()ی OrderItem به‌طور پیش‌فرض unit_price را با فرمول قدیمی
        # تک‌محوره‌ی calculate_price() بازنویسی می‌کند. قیمت اینجا از
        # همین سبد و با storefront.pricing (منبع واحد محاسبه‌ی ابعاد)
        # محاسبه شده، پس بازنویسی را غیرفعال می‌کنیم.
        order_item._skip_price_calc = True
        order_item.save()
        # رنگ‌های پیش‌فرض محصول به آیتم سفارش منتقل می‌شوند تا بعداً
        # انبار و نقاشی بتوانند از آن استفاده کنند.
        for part, code in (product.default_colors or {}).items():
            if not code or code == 'nan':
                continue
            Color.objects.get_or_create(
                part=part, code=str(code), orderitem=order_item,
            )
        total += order_item.line_total

    order.total_amount = total
    order.discount = cart.discount
    order.discount_amount = cart.discount_amount
    order.final_amount = cart.final_price
    order.save(update_fields=['total_amount', 'discount', 'discount_amount', 'final_amount'])

    cart.items.all().delete()

    # صدور دستور تولید با منطق خود CraftFlow — نه نسخه‌ی قدیمی پروژه‌ی store.
    # این همان مسیری است که با صف روزانه‌ی انبار و زمان‌بندی نقاشی هماهنگ است.
    result = order.generate_tasks()
    if not result.get('success'):
        # سفارش ثبت شده، اما تولید نمی‌تواند شروع شود؛ مشتری باید بداند
        # پیگیری می‌شود.
        messages.warning(
            request,
            f"سفارش ثبت شد، اما صدور دستور تولید با مشکل مواجه شد: {result.get('error')}",
        )
    else:
        messages.success(request, f'سفارش شما با شماره {order.id} ثبت شد.')
    return redirect('storefront:order_detail', order_id=order.id)


@login_required
def order_list(request):
    orders = (
        Order.objects.filter(user=request.user)
        .select_related('customer')
        .prefetch_related('items__packaging_units')
        .order_by('-id')
    )
    return render(request, 'storefront/order_list.html', {'orders': orders})


def _status_timeline(order):
    """مراحل نمایشی سفارش برای timeline صفحه‌ی جزئیات.

    وضعیت‌های مالی/تولیدی سفارش را به پنج مرحه‌ی
    «ثبت ← پرداخت ← تولید ← ارسال ← تحویل» نگاشت می‌کنیم."""
    paid = order.status in ('paid', 'shipped', 'delivered') or order.paid_at is not None
    producing = order.status in ('producing', 'completed', 'shipped', 'delivered')
    shipped = order.status in ('shipped', 'delivered')
    delivered = order.status == 'delivered'
    steps = [
        {'label': 'ثبت سفارش', 'done': True},
        {'label': 'پرداخت', 'done': paid},
        {'label': 'تولید', 'done': producing},
        {'label': 'ارسال', 'done': shipped},
        {'label': 'تحویل', 'done': delivered},
    ]
    for i, step in enumerate(steps):
        # خطِ بعد از هر مرحله، وقتی مرحله‌ی بعدی انجام شده رنگ می‌شود.
        step['line_done'] = i + 1 < len(steps) and steps[i + 1]['done']
    return steps


@login_required
def order_detail(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related('customer', 'payment').prefetch_related(
            'items__product__category', 'items__ordercolor', 'items__packaging_units',
        ),
        pk=order_id,
        user=request.user,
    )
    # وضعیت‌های قفل‌شده و نمایش دکمه‌ی پرداخت را اینجا محاسبه می‌کنیم
    # چون قالب جنگو از `in` روی لیست و پرانتز در `if` پشتیبانی نمی‌کند.
    is_locked = order.status in Order.LOCKED_STATUSES
    cod_pending = bool(
        getattr(order, 'payment', None)
        and order.payment.gateway == 'cash_on_delivery'
        and order.payment.status == 'pending'
    )
    show_pay_button = not is_locked and not cod_pending
    return render(request, 'storefront/order_detail.html', {
        'order': order,
        'is_locked': is_locked,
        'show_pay_button': show_pay_button,
        'is_cancelled': order.status == 'cancelled',
        'timeline': _status_timeline(order),
    })
