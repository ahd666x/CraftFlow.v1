from django.conf import settings
from django.db import models
from product.models import Material, Order


class SheetStock(models.Model):
    """ورق‌های در دسترس برش؛ ابعاد بر حسب میلی‌متر است."""
    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name='cut_sheet_stock')
    width = models.PositiveIntegerField(verbose_name='عرض (میلی‌متر)')
    height = models.PositiveIntegerField(verbose_name='طول (میلی‌متر)')
    quantity = models.PositiveIntegerField(default=1, verbose_name='تعداد')
    is_offcut = models.BooleanField(default=False, verbose_name='پرت قابل استفاده')
    grain = models.CharField(max_length=100, blank=True, verbose_name='جهت رگه')
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['is_offcut', 'width', 'height']
        verbose_name = 'موجودی ورق برش'
        verbose_name_plural = 'موجودی ورق‌های برش'
    def __str__(self):
        return f'{self.material}: {self.width}×{self.height} ({self.quantity})'


class CutPlan(models.Model):
    STATUS = [('planned', 'برنامه‌ریزی'), ('cut', 'برش انجام شد')]
    orders = models.ManyToManyField(Order, related_name='cut_plans')
    status = models.CharField(max_length=12, choices=STATUS, default='planned')
    plan_data = models.JSONField(default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    cut_at = models.DateTimeField(null=True, blank=True)
