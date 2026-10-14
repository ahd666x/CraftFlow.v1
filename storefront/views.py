from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static

from cart.services import get_or_create_cart
from product.models import Color, Customer, Order, OrderItem, Product, ProductCategory
from storefront.models import Address, HeroSlide, ProductReview, SiteSettings


def manifest(request):
    s = SiteSettings.objects.first() or SiteSettings.get_settings()
    icon = s.pwa_icon.url if s.pwa_icon else static('storefront/img/icon-512.png')
    data = {
        'name': s.site_name,
        'short_name': s.short_name or s.site_name,
        'description': s.short_description,
        'start_url': '/shop/',
        'scope': '/shop/',
        'display': 'standalone',
        'dir': 'rtl',
        'lang': 'fa',
        'background_color': '#ffffff',
        'theme_color': s.primary_color,
        'icons': [
            {'src': icon, 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any maskable'},
            {'src': icon, 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any maskable'},
        ],
    }
    return JsonResponse(data, json_dumps_params={'ensure_ascii': False},
                        content_type='application/manifest+json')


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
    # Hero slides (admin-managed)
    hero_slides = HeroSlide.objects.filter(is_active=True).order_by('order')
    
    # Products for carousel (latest 8)
    products = _with_rating(
        Product.active_objects.select_related('category')
        .prefetch_related('gallery_images')
        .order_by('-id')[:8]
    )
    
    # Categories for bento grid (top 6 with product count)
    categories = ProductCategory.objects.filter(is_active=True).annotate(
        product_count=Count('products', filter=Q(products__is_active=True)),
    ).order_by('-product_count')[:6]
    
    # Features for trust bar - short descriptions (max 6 words)
    site_settings = SiteSettings.get_settings()
    features = [
        ('custom', 'ابعاد دلخواه', 'طول عرض ارتفاع دقیقاً متناسب'),
        ('quality', 'چوب درجه یک، ضد رطوبت و بادوام', 'چوب درجه یک ضد رطوبت دوام‌دار'),
        ('shipping', 'ارسال سراسری', 'بسته‌بندی ایمن سراسر ایران'),
    ]
    if site_settings.warranty_years:
        features.insert(2, ('warranty', f'گارانتی {site_settings.warranty_years} سال', 'اطمینان کیفیت ساخت مواد'))
    
    # Testimonials: real reviews with rating >= 4
    testimonials = []
    recent_reviews = ProductReview.objects.filter(
        is_active=True, rating__gte=4
    ).select_related('user', 'product').order_by('-created_at')[:6]
    for review in recent_reviews:
        testimonials.append({
            'name': review.user.get_full_name() or review.user.username,
            'product': review.product.name,
            'comment': review.comment or f'امتیاز {review.rating} از ۵',
        })
    
    return render(request, 'storefront/home.html', {
        'hero_slides': hero_slides,
        'products': products,
        'categories': categories,
        'features': features,
        'testimonials': testimonials,
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

    # Price range filter
    min_price = request.GET.get('min_price')
    max_price = request.GET.get('max_price')
    if min_price:
        try:
            queryset = queryset.filter(base_price__gte=int(min_price))
        except ValueError:
            pass
    if max_price:
        try:
            queryset = queryset.filter(base_price__lte=int(max_price))
        except ValueError:
            pass

    sort = request.GET.get('sort', '')
    queryset = _sort_queryset(queryset, sort)

    # Pagination: 12 products per page
    paginator = Paginator(_with_rating(queryset), 12)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Popular products for empty state (most reviewed)
    popular_products = _with_rating(
        Product.active_objects
        .filter(reviews__is_active=True)
        .select_related('category')
        .prefetch_related('gallery_images')
    ).order_by('-rev_count').distinct()[:6]

    current_category = None
    if category_slug:
        current_category = ProductCategory.objects.filter(slug=category_slug, is_active=True).first()

    return render(request, 'storefront/product_list.html', {
        'page_obj': page_obj,
        'categories': ProductCategory.objects.filter(is_active=True),
        'q': q,
        'selected_category': category_slug,
        'current_category': current_category,
        'sort': sort,
        'min_price': min_price,
        'max_price': max_price,
        'popular_products': popular_products,
    })


def product_detail(request, slug):
    from product.utils import _parse_default_colors, get_color_code_choices, get_color_hex_map

    product = get_object_or_404(
        _with_rating(Product.active_objects.select_related('category')),
        slug=slug,
    )
    gallery = product.gallery_images.filter(is_active=True)

    # محصولات پیشنهادی: هم‌نام‌های همین محصول از دیگر دسته‌بندی‌ها
    # (مثلاً برای «جلومبل فیدان»، محصولات «فیدان» در کنسول، عسلی و...).
    # اگر هم‌نامی در دسته‌ای دیگر نبود، به محصولات همان دسته برمی‌گردیم.
    related_products = _with_rating(
        Product.active_objects
        .filter(name=product.name)
        .exclude(category=product.category)
        .select_related('category')
        .prefetch_related('gallery_images')
        .order_by('category_id', '-id')[:6]
    )
    related_by_name = bool(related_products)
    if not related_products:
        related_products = _with_rating(
            Product.active_objects
            .filter(category=product.category)
            .exclude(pk=product.pk)
            .select_related('category')
            .prefetch_related('gallery_images')
            .order_by('-id')[:6]
        )

    reviews = product.reviews.filter(is_active=True).select_related('user')

    default_colors = _parse_default_colors(product)

    # بخش‌های رنگی قابل انتخاب، به ترتیب استاندارد PART_CHOICES.
    # کدهای خالی/نامعتبر (مثل nan) از پیش‌فرض حذف می‌شوند؛ اگر کد
    # پیش‌فرض در تعریف‌های فعال نباشد، اولین گزینه انتخاب می‌شود.
    valid_codes = {code for code, _label in get_color_code_choices()}
    color_parts = []
    for part, _label in Color.PART_CHOICES:
        code = str(default_colors.get(part, '') or '').strip()
        if not code or code.lower() == 'nan':
            continue
        color_parts.append({
            'part': part,
            'default_code': code if code in valid_codes else '',
        })

    context = {
        'product': product,
        'gallery': gallery,
        'default_colors': default_colors,
        'color_parts': color_parts,
        'color_code_choices': [code for code, _label in get_color_code_choices()],
        'color_hex_map': get_color_hex_map(),
        'related_products': related_products,
        'related_by_name': related_by_name,
        'reviews': reviews,
    }
    return render(request, 'storefront/product_detail.html', context)


@login_required
def review_create(request, slug):
    """Create a review for a product."""
    if request.method != 'POST':
        return redirect('storefront:product_detail', slug=slug)

    product = get_object_or_404(Product.active_objects, slug=slug)
    
    # Check if user already reviewed this product
    existing = ProductReview.objects.filter(product=product, user=request.user).first()
    if existing:
        messages.error(request, 'شما قبلاً برای این محصول نظر داده‌اید.')
        if request.headers.get('HX-Request'):
            from django.http import HttpResponse
            return HttpResponse('شما قبلاً برای این محصول نظر داده‌اید.', status=422)
        return redirect('storefront:product_detail', slug=slug)

    try:
        rating = int(request.POST.get('rating', 0))
    except (TypeError, ValueError):
        rating = 0
    
    if rating < 1 or rating > 5:
        messages.error(request, 'امتیاز باید بین ۱ تا ۵ باشد.')
        if request.headers.get('HX-Request'):
            from django.http import HttpResponse
            return HttpResponse('امتیاز باید بین ۱ تا ۵ باشد.', status=422)
        return redirect('storefront:product_detail', slug=slug)

    comment = request.POST.get('comment', '').strip()
    image = request.FILES.get('image')

    review = ProductReview.objects.create(
        product=product,
        user=request.user,
        rating=rating,
        comment=comment,
        image=image,
        is_active=False,  # نیاز به تایید ادمین
    )

    messages.success(request, 'نظر شما با موفقیت ثبت شد و پس از تایید نمایش داده می‌شود.')
    
    if request.headers.get('HX-Request'):
        # Return updated reviews list for HTMX
        reviews = product.reviews.filter(is_active=True).select_related('user')
        return render(request, 'storefront/partials/reviews_list.html', {'reviews': reviews, 'product': product})
    
    return redirect('storefront:product_detail', slug=slug)


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
    
    # Price range filter
    min_price = request.GET.get('min_price')
    max_price = request.GET.get('max_price')
    if min_price:
        try:
            queryset = queryset.filter(base_price__gte=int(min_price))
        except ValueError:
            pass
    if max_price:
        try:
            queryset = queryset.filter(base_price__lte=int(max_price))
        except ValueError:
            pass

    sort = request.GET.get('sort', '')
    queryset = _sort_queryset(queryset, sort)

    # Pagination: 12 products per page
    paginator = Paginator(_with_rating(queryset), 12)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'storefront/product_list.html', {
        'page_obj': page_obj,
        'categories': ProductCategory.objects.filter(is_active=True),
        'selected_category': slug,
        'current_category': category,
        'q': q,
        'sort': sort,
        'min_price': min_price,
        'max_price': max_price,
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
    
    # Validate phone number format (same as frontend: ^09\d{9}$)
    import re
    if not phone:
        messages.error(request, 'شماره موبایل الزامی است.')
        return redirect('storefront:checkout')
    if not re.match(r'^09\d{9}$', phone):
        messages.error(request, 'شماره موبایل باید با ۰۹ شروع شود و ۱۱ رقم باشد.')
        return redirect('storefront:checkout')

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
        # رنگ‌های انتخاب‌شده توسط مشتری (و در نبودِ آن، رنگ‌های پیش‌فرض
        # محصول) به آیتم سفارش منتقل می‌شوند تا بعداً انبار و نقاشی
        # بتوانند از آن استفاده کنند.
        from product.utils import _parse_default_colors
        item_colors = item.selected_colors if isinstance(item.selected_colors, dict) and item.selected_colors else None
        if item_colors is None:
            item_colors = _parse_default_colors(product)
        for part, code in item_colors.items():
            if not code or str(code).lower() == 'nan':
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

    # Increment discount used_count if discount was applied
    if cart.discount:
        cart.discount.used_count += 1
        cart.discount.save(update_fields=['used_count'])

    cart.items.all().delete()
    # Clear cart discount after order is placed
    cart.discount = None
    cart.save(update_fields=['discount'])

    # NOTE: generate_tasks() is now called after successful payment in _finish_payment
    # to ensure payment happens before production tasks are generated.
    messages.success(request, f'سفارش شما با شماره {order.id} ثبت شد. برای تکمیل خرید به درگاه پرداخت هدایت می‌شوید.')
    return redirect('payments:payment_create', order_id=order.id)


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


def custom_404(request, exception):
    """Custom 404 error handler."""
    return render(request, 'storefront/404.html', status=404)


def custom_500(request):
    """Custom 500 error handler."""
    return render(request, 'storefront/500.html', status=500)


def about(request):
    """About Us page."""
    return render(request, 'storefront/about.html', {
        'site_settings': SiteSettings.get_settings(),
    })


def return_policy(request):
    """Return Policy page."""
    return render(request, 'storefront/return_policy.html', {
        'site_settings': SiteSettings.get_settings(),
    })


def terms_of_service(request):
    """Terms of Service page."""
    return render(request, 'storefront/terms_of_service.html', {
        'site_settings': SiteSettings.get_settings(),
    })
