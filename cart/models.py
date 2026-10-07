from django.conf import settings
from django.db import models

from storefront.base import TimeStampedModel


class Cart(TimeStampedModel):
    """
    سبد خرید برای کاربر واردشده یا مهمان.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='cart', null=True, blank=True, verbose_name="کاربر",
    )
    session_key = models.CharField(max_length=40, null=True, blank=True, db_index=True, verbose_name="کلید جلسه")
    discount = models.ForeignKey(
        'discounts.Discount', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='carts', verbose_name="تخفیف",
    )

    class Meta:
        verbose_name = "سبد خرید"
        verbose_name_plural = "سبدهای خرید"

    def __str__(self):
        if self.user:
            return f"سبد خرید {self.user.username}"
        return f"سبد خرید مهمان ({self.session_key})"

    @property
    def total_items(self):
        return sum(item.quantity for item in self.items.all())

    @property
    def discount_amount(self):
        """مبلغ تخفیف محاسبه‌شده بر اساس کد فعلی سبد."""
        if not self.discount or not self.discount.is_valid:
            return 0
        total = self.total_price
        if self.discount.discount_type == 'percent':
            return int(total * self.discount.value / 100)
        return min(self.discount.value, total)

    @property
    def total_price(self):
        return sum(item.total_price for item in self.items.all())

    @property
    def final_price(self):
        return max(self.total_price - self.discount_amount, 0)


class CartItem(TimeStampedModel):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='items', verbose_name="سبد خرید")
    product = models.ForeignKey('product.Product', on_delete=models.CASCADE, verbose_name="محصول")
    quantity = models.PositiveIntegerField(default=1, verbose_name="ت数量")

    # ابعاد سفارشی انتخاب‌شده توسط مشتری در صفحه جزئیات محصول.
    # اگر خالی باشند یعنی مشتری ابعاد پیش‌فرض محصول را پذیرفته است.
    custom_length = models.PositiveIntegerField(null=True, blank=True, verbose_name="طول سفارشی (سانتی‌متر)")
    custom_width = models.PositiveIntegerField(null=True, blank=True, verbose_name="عرض سفارشی (سانتی‌متر)")
    custom_height = models.PositiveIntegerField(null=True, blank=True, verbose_name="ارتفاع سفارشی (سانتی‌متر)")

    # قیمت واحد محاسبه‌شده بر اساس ابعاد سفارشی، در لحظه افزودن/ویرایش
    # ذخیره می‌شود تا اگر قیمت پایه محصول بعداً تغییر کرد، قیمت داخل سبد
    # کاربر همان چیزی بماند که در لحظه انتخاب دیده است.
    unit_price = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name="قیمت واحد")

    class Meta:
        verbose_name = "آیتم سبد خرید"
        verbose_name_plural = "آیتم‌های سبد خرید"
        # توجه: unique_together ندارد، چون یک محصول می‌تواند با ابعاد
        # سفارشی متفاوت چند بار در سبد باشد.

    def __str__(self):
        return f"{self.product.name} - {self.quantity}"

    def recalculate_price(self):
        """قیمت واحد را بر اساس ابعاد سفارشی فعلی دوباره محاسبه می‌کند."""
        from storefront.pricing import calculate_dimension_price
        self.unit_price = calculate_dimension_price(
            base_price=self.product.base_price,
            default_length=self.product.length,
            default_width=self.product.width,
            default_height=self.product.height,
            length_percent=self.product.length_price_percent,
            width_percent=self.product.width_price_percent,
            height_percent=self.product.height_price_percent,
            length_editable=self.product.length_editable,
            width_editable=self.product.width_editable,
            height_editable=self.product.height_editable,
            length=self.custom_length,
            width=self.custom_width,
            height=self.custom_height,
        )

    @property
    def total_price(self):
        return (self.unit_price or 0) * self.quantity

    @property
    def has_custom_dimensions(self):
        return any([self.custom_length, self.custom_width, self.custom_height])