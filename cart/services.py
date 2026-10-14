from django.db import transaction
from django.shortcuts import get_object_or_404

from cart.models import Cart, CartItem
from product.models import Product


def _normalize_dim(value):
    """Return an int for truthy dimension inputs, or None for empty/blank.

    The storefront form submits an empty string '' when the customer leaves a
    dimension blank; ``None`` is what the model field stores. Both must be
    treated as "use the product default".
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None




MAX_CUSTOM_DIMENSION_CM = 1000


def _resolve_dim(raw, default, editable, label):
    """Return the dimension to store for a cart item.

    When the axis is not editable the product default always wins, even if the
    client sent a different value. When it is editable the submitted value is
    validated against the 1..MAX_CUSTOM_DIMENSION_CM range; out-of-range values
    raise ``ValueError`` so the caller can surface a friendly message.
    """
    if not editable:
        return default
    # An explicit 0 is treated as an out-of-range value, not as "use the
    # default" — only an empty/blank input falls back to the default.
    # Handle both string '0' and integer 0
    if raw is not None:
        raw_str = str(raw).strip()
        if raw_str == '0' or raw == 0:
            raise ValueError(f'{label} باید بین ۱ تا {MAX_CUSTOM_DIMENSION_CM} سانتیمتر باشد.')
    value = _normalize_dim(raw)
    if value is None:
        return default
    if value < 1 or value > MAX_CUSTOM_DIMENSION_CM:
        raise ValueError(f'{label} باید بین ۱ تا {MAX_CUSTOM_DIMENSION_CM} سانتیمتر باشد.')
    return value


def _resolve_colors(product, colors):
    """کدهای رنگ انتخاب‌شده را اعتبارسنجی و نرمال‌سازی می‌کند.

    فقط بخش‌هایی که در رنگ‌های پیش‌فرض محصول تعریف شده‌اند و فقط
    کدهایی که در تعریف‌های کد رنگ فعال موجودند پذیرفته می‌شوند.
    کلیدها مرتب می‌شوند تا سریال‌سازی JSON (و در نتیجه برابری در
    دیتابیس و unique constraint) قطعی باشد.
    """
    from product.utils import get_color_code_choices, _parse_default_colors

    if not colors:
        return {}
    if not isinstance(colors, dict):
        return {}
    allowed_parts = set(_parse_default_colors(product).keys())
    valid_codes = {str(code) for code, _label in get_color_code_choices()}
    resolved = {}
    for part, code in colors.items():
        part = str(part).strip()
        code = str(code).strip() if code is not None else ''
        if not part or not code or part not in allowed_parts or code not in valid_codes:
            continue
        resolved[part] = code
    return dict(sorted(resolved.items()))

def get_or_create_cart(request):
    """
    سبد کاربر را برمی‌گرداند و در نبودِ آن می‌سازد.

    برای مهمان، سبد به `session_key` گره می‌خورد. نکته‌ی مهم: برای مهمان
    باید session را *قبل* از ساخت سبد فعال کنیم، وگرنه `session_key`
    خالی می‌ماند و سبد دو بار ساخته می‌شود.

    تفاوت با مبدأ: اینجا `stock` نداریم (کالای ما تولید-به‌موقع است)، پس
    هیچ سقفی روی تعداد اعمال نمی‌شود.
    """
    if request.user.is_authenticated:
        cart, _ = Cart.objects.get_or_create(user=request.user)
        return cart

    if not request.session.session_key:
        request.session.save()
    cart, _ = Cart.objects.get_or_create(session_key=request.session.session_key)
    return cart


def merge_guest_cart_to_user(request, user):
    """
    Merge guest cart (session-based) into user cart after login.
    
    This should be called after successful authentication.
    """
    if not request.session.session_key:
        return
    
    try:
        guest_cart = Cart.objects.get(session_key=request.session.session_key)
    except Cart.DoesNotExist:
        return
    
    user_cart, _ = Cart.objects.get_or_create(user=user)
    
    # Merge items from guest cart to user cart
    for guest_item in guest_cart.items.select_related('product').all():
        user_item, created = CartItem.objects.select_for_update().get_or_create(
            cart=user_cart,
            product=guest_item.product,
            custom_length=guest_item.custom_length,
            custom_width=guest_item.custom_width,
            custom_height=guest_item.custom_height,
            selected_colors=guest_item.selected_colors or {},
            defaults={'quantity': guest_item.quantity}
        )
        if not created:
            user_item.quantity += guest_item.quantity
        user_item.recalculate_price()
        user_item.save()
    
    # Merge discount if guest cart has one and user cart doesn't
    if guest_cart.discount and not user_cart.discount:
        user_cart.discount = guest_cart.discount
        user_cart.save(update_fields=['discount'])
    
    # Delete guest cart
    guest_cart.delete()
    
    # Clear session key to prevent re-merging
    request.session.flush()


class CartService:
    @staticmethod
    @transaction.atomic
    def add_item(cart, product_id, quantity=1, length=None, width=None, height=None, colors=None):
        """
        آیتم را به سبد اضافه می‌کند. اگر ابعاد سفارشی داده نشود، ابعاد
        پیش‌فرم محصول جایگزین می‌شود (یعنی اختلاف قیمت صفر و قیمت = قیمت پایه).
        کدهای رنگ انتخاب‌شده برای بخش‌های رنگی محصول هم در آیتم ذخیره
        می‌شوند؛ ترکیب متفاوتِ رنگ‌ها یک آیتم جداگانه می‌سازد.
        """
        product = get_object_or_404(Product, id=product_id, is_active=True)

        # فرم صفحه جزئیات محصول رشته خالی '' را ارسال می‌کند اگر کاربر چیزی وارد
        # نکرده باشد؛ هر دو حالت '' و None باید به پیش‌فرم محصول برگردند.
        length = _resolve_dim(length, product.length, product.length_editable, 'length')
        width = _resolve_dim(width, product.width, product.width_editable, 'width')
        height = _resolve_dim(height, product.height, product.height_editable, 'height')
        colors = _resolve_colors(product, colors)

        cart_item, created = CartItem.objects.select_for_update().get_or_create(
            cart=cart, product=product,
            custom_length=length, custom_width=width, custom_height=height,
            selected_colors=colors,
            defaults={'quantity': quantity}
        )

        if not created:
            cart_item.quantity += quantity

        cart_item.recalculate_price()
        cart_item.save()
        cart._clear_cached_properties()
        return cart_item

    @staticmethod
    @transaction.atomic
    def remove_item(cart, item_id):
        deleted, _ = CartItem.objects.filter(cart=cart, pk=item_id).delete()
        if deleted:
            cart._clear_cached_properties()
        return deleted

    @staticmethod
    @transaction.atomic
    def update_quantity(cart, item_id, quantity):
        item = CartItem.objects.filter(cart=cart, pk=item_id).first()
        if item is None:
            return 0
        if quantity <= 0:
            item.delete()
            cart._clear_cached_properties()
            return 1
        item.quantity = quantity
        item.recalculate_price()
        item.save(update_fields=['quantity', 'unit_price', 'updated_at'])
        cart._clear_cached_properties()
        return 1

    @staticmethod
    @transaction.atomic
    def clear(cart):
        cart.items.all().delete()
        cart._clear_cached_properties()
