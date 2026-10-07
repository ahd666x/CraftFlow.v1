from django.db import transaction
from django.shortcuts import get_object_or_404

from cart.models import Cart, CartItem
from product.models import Product


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


class CartService:
    @staticmethod
    @transaction.atomic
    def add_item(cart, product_id, quantity=1, length=None, width=None, height=None):
        """
        آیتم را به سبد اضافه می‌کند. اگر ابعاد سفارشی داده نشود، ابعاد
        پیش‌فرض محصول جایگزین می‌شود (یعنی اختلاف قیمت صفر و قیمت = قیمت پایه).
        """
        product = get_object_or_404(Product, id=product_id, is_active=True)

        length = length if length is not None else product.length
        width = width if width is not None else product.width
        height = height if height is not None else product.height

        cart_item = CartItem.objects.filter(
            cart=cart, product=product,
            custom_length=length, custom_width=width, custom_height=height,
        ).first()

        if cart_item:
            cart_item.quantity += quantity
        else:
            cart_item = CartItem(
                cart=cart, product=product, quantity=quantity,
                custom_length=length, custom_width=width, custom_height=height,
            )

        cart_item.recalculate_price()
        cart_item.save()
        return cart_item

    @staticmethod
    @transaction.atomic
    def remove_item(cart, product_id):
        """همه‌ی ردیف‌های آن محصول را حذف می‌کند (چون ابعاد می‌تواند متفاوت باشد)."""
        deleted, _ = CartItem.objects.filter(cart=cart, product_id=product_id).delete()
        return deleted

    @staticmethod
    @transaction.atomic
    def update_quantity(cart, product_id, quantity):
        """تعداد یک محصول را به‌روز می‌کند. تعداد صفر یعنی حذف."""
        if quantity <= 0:
            return CartService.remove_item(cart, product_id)
        updated = 0
        for item in CartItem.objects.filter(cart=cart, product_id=product_id):
            item.quantity = quantity
            item.recalculate_price()
            item.save(update_fields=['quantity', 'unit_price', 'updated_at'])
            updated += 1
        return updated

    @staticmethod
    @transaction.atomic
    def clear(cart):
        cart.items.all().delete()
