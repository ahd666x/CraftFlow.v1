"""
مدل تخفیف.

کد تخفیف یک رکورد مستقل است؛ سبد و سفارش به آن ارجاع می‌دهند.
"""
from django.db import models

from storefront.base import TimeStampedModel


class Discount(TimeStampedModel):
    DISCOUNT_TYPE_CHOICES = [
        ('percent', 'درصدی'),
        ('fixed', 'ثابت'),
    ]

    code = models.CharField(max_length=50, unique=True, verbose_name="کد تخفیف")
    discount_type = models.CharField(
        max_length=10, choices=DISCOUNT_TYPE_CHOICES, default='percent',
        verbose_name="نوع تخفیف",
    )
    value = models.PositiveIntegerField(verbose_name="مقدار تخفیف")
    max_uses = models.PositiveIntegerField(default=1, verbose_name="حداکثر استفاده")
    used_count = models.PositiveIntegerField(default=0, verbose_name="تعداد استفاده")
    valid_from = models.DateTimeField(verbose_name="اعتبار از")
    valid_until = models.DateTimeField(verbose_name="اعتبار تا")
    is_active = models.BooleanField(default=True, verbose_name="عالی")

    class Meta:
        verbose_name = "تخفیف"
        verbose_name_plural = "تخفیف‌ها"

    def __str__(self):
        return f"{self.code} - {self.get_discount_type_display()} {self.value}"

    @property
    def is_valid(self):
        from django.utils import timezone
        now = timezone.now()
        within_dates = self.valid_from <= now <= self.valid_until
        usage_ok = self.max_uses == 0 or self.used_count < self.max_uses
        return self.is_active and within_dates and usage_ok