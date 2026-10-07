"""
مدل‌های پرداخت.

این ماژول مستقل است و هیچ رابطه‌ای با مدل‌های `product` ندارد — فقط به
`Order` ارجاع می‌دهیم. `Payment` یک رکورد یک‌تایی برای هر سفارش ایجاد می‌کند
و `Transaction` رکورد هر تلاش پرداخت (مثلاً verify قراردادی زرین‌پال) است.
"""
from django.db import models

from storefront.base import TimeStampedModel


class Payment(TimeStampedModel):
    STATUS_CHOICES = [
        ('pending', 'در انتظار'),
        ('success', 'موفق'),
        ('failed', 'ناموفق'),
        ('cancelled', 'لغو شده'),
    ]

    order = models.OneToOneField(
        'product.Order', on_delete=models.CASCADE, related_name='payment',
        verbose_name="سفارش",
    )
    amount = models.DecimalField(max_digits=15, decimal_places=0, verbose_name="مبلغ")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="وضعیت")
    gateway = models.CharField(max_length=50, default='zarinpal', verbose_name="درگاه پرداخت")
    transaction_id = models.CharField(max_length=100, blank=True, verbose_name="شناسه تراکنش")
    authority = models.CharField(max_length=100, blank=True, verbose_name="کد-authority")
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name="تاریخ پرداخت")

    class Meta:
        verbose_name = "پرداخت"
        verbose_name_plural = "پرداخت‌ها"

    def __str__(self):
        return f"پرداخت سفارش #{self.order.id} - {self.get_status_display()}"


class Transaction(TimeStampedModel):
    payment = models.ForeignKey(
        Payment, on_delete=models.CASCADE, related_name='transactions',
        verbose_name="پرداخت",
    )
    amount = models.DecimalField(max_digits=15, decimal_places=0, verbose_name="مبلغ")
    ref_id = models.CharField(max_length=100, blank=True, verbose_name="شماره مرجع")
    card_pan = models.CharField(max_length=20, blank=True, verbose_name="شماره کارت")

    class Meta:
        verbose_name = "traکنش"
        verbose_name_plural = "traکنش‌ها"

    def __str__(self):
        return f"traکنش {self.ref_id or self.id}"