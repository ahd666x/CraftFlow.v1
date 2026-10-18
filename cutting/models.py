from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from product.models import Material, Order, ProductionTask


MM_FIELD = {'max_digits': 10, 'decimal_places': 1}


class SheetSizeSpec(models.Model):
    """Reusable standard sheet format; changing it never alters existing stock."""
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='cut_sheet_sizes')
    name = models.CharField(max_length=100)
    width = models.DecimalField(verbose_name='عرض (میلی‌متر)', **MM_FIELD)
    height = models.DecimalField(verbose_name='طول (میلی‌متر)', **MM_FIELD)
    active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ('material__name', 'name', 'width', 'height')
        constraints = [
            models.UniqueConstraint(fields=('material', 'name'), name='cut_size_material_name_uniq'),
            models.UniqueConstraint(fields=('material',), condition=Q(is_default=True, active=True), name='cut_one_default_size_per_material'),
            models.CheckConstraint(condition=Q(width__gt=0) & Q(height__gt=0), name='cut_size_dimensions_positive'),
        ]
        verbose_name = 'اندازه استاندارد ورق'
        verbose_name_plural = 'اندازه‌های استاندارد ورق'
    def __str__(self):
        return f'{self.material.name} — {self.name} ({self.width}×{self.height})'


class SheetStock(models.Model):
    """One intake batch of full sheets or usable offcuts; dimensions are millimetres."""
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='cut_sheet_stock')
    width = models.DecimalField(verbose_name='عرض (میلی‌متر)', **MM_FIELD)
    height = models.DecimalField(verbose_name='طول (میلی‌متر)', **MM_FIELD)
    quantity = models.PositiveIntegerField(default=1, verbose_name='تعداد')
    is_offcut = models.BooleanField(default=False, verbose_name='پرت قابل استفاده')
    active = models.BooleanField(default=True, verbose_name='فعال')
    grain = models.CharField(max_length=100, blank=True, verbose_name='جهت رگه')
    priority = models.IntegerField(default=100, verbose_name='اولویت مصرف (کمتر = زودتر)')
    note = models.CharField(max_length=255, blank=True, verbose_name='یادداشت')
    inactive_reason = models.CharField(max_length=255, blank=True, verbose_name='علت غیرفعال‌سازی')
    origin = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='offspring_offcuts')
    created_from_plan = models.ForeignKey('CutPlan', null=True, blank=True, on_delete=models.SET_NULL, related_name='generated_offcuts')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='cut_stock_batches')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ('priority', 'is_offcut', 'width', 'height', 'pk')
        indexes = [models.Index(fields=('material', 'active', 'is_offcut'))]
        constraints = [models.CheckConstraint(condition=Q(width__gt=0) & Q(height__gt=0), name='cut_stock_dimensions_positive')]
        verbose_name = 'بچ موجودی ورق برش'
        verbose_name_plural = 'موجودی ورق‌های برش'
    def __str__(self):
        kind = 'پرت' if self.is_offcut else 'کامل'
        return f'{self.material} — {kind} {self.width}×{self.height} ({self.quantity})'


class CuttingSettings(models.Model):
    STRATEGIES = [('offcuts_first', 'ابتدا پرت، سپس ورق کامل'), ('full_only', 'فقط ورق کامل'), ('priority', 'اولویت دستی موجودی')]
    strategy = models.CharField(max_length=20, choices=STRATEGIES, default='offcuts_first')
    default_kerf = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal('4.0'), verbose_name='ضخامت تیغه پیش‌فرض (mm)')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        verbose_name = 'تنظیم سیاست مصرف ورق'
    @classmethod
    def current(cls):
        return cls.objects.get_or_create(pk=1)[0]


class CutPlan(models.Model):
    STATUS = [('planned', 'برنامه‌ریزی'), ('cut', 'برش انجام شد'), ('partial', 'نتیجه جزئی؛ ادامه لازم است'), ('cancelled', 'لغو شد')]
    orders = models.ManyToManyField(Order, related_name='cut_plans')
    status = models.CharField(max_length=12, choices=STATUS, default='planned')
    plan_data = models.JSONField(default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    cut_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        ordering = ('-created_at',)


class CutPlanTask(models.Model):
    """Exact task membership and planned remainder are snapshotted for safe partial runs."""
    plan = models.ForeignKey(CutPlan, on_delete=models.CASCADE, related_name='planned_tasks')
    task = models.ForeignKey(ProductionTask, on_delete=models.PROTECT, related_name='cut_plan_memberships')
    quantity = models.PositiveIntegerField()
    task_quantity = models.PositiveIntegerField(default=1, editable=False, verbose_name='کل تعداد تسک هنگام برنامه‌ریزی')
    completed_before = models.PositiveIntegerField(default=0, editable=False, verbose_name='تعداد تکمیل‌شده پیشین')
    class Meta:
        constraints = [models.UniqueConstraint(fields=('plan', 'task'), name='cutplan_task_once')]


class CutPlanSheet(models.Model):
    OUTCOMES = [('reserved', 'در انتظار نتیجه'), ('used', 'مصرف شد'), ('untouched', 'استفاده نشد'), ('scrapped', 'غیرقابل استفاده'), ('released', 'رزرو آزاد شد؛ برنامه لغو شد')]
    plan = models.ForeignKey(CutPlan, on_delete=models.CASCADE, related_name='planned_sheets')
    source_stock = models.ForeignKey(SheetStock, on_delete=models.PROTECT, related_name='plan_reservations')
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='cut_plan_sheets')
    layout_index = models.PositiveIntegerField()
    width = models.DecimalField(verbose_name='عرض (میلی‌متر)', **MM_FIELD)
    height = models.DecimalField(verbose_name='طول (میلی‌متر)', **MM_FIELD)
    was_offcut = models.BooleanField(default=False)
    outcome = models.CharField(max_length=12, choices=OUTCOMES, default='reserved')
    actual_offcuts = models.JSONField(default=list, blank=True)
    result_note = models.CharField(max_length=255, blank=True)
    class Meta:
        ordering = ('material__name', 'layout_index')
        constraints = [
            models.UniqueConstraint(fields=('plan', 'material', 'layout_index'), name='cutplan_sheet_layout_once'),
            models.CheckConstraint(condition=Q(width__gt=0) & Q(height__gt=0), name='cutplan_sheet_dimensions_positive'),
        ]
        indexes = [models.Index(fields=('source_stock', 'outcome'))]


class CutPlanSheetTask(models.Model):
    """Immutable-at-finalization snapshot of how many pieces for each task were laid on a sheet."""
    plan_sheet = models.ForeignKey(CutPlanSheet, on_delete=models.CASCADE, related_name='task_counts')
    task = models.ForeignKey(ProductionTask, on_delete=models.PROTECT, related_name='cut_plan_sheet_counts')
    quantity = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('plan_sheet', 'task'), name='cutplansheet_task_once'),
            models.CheckConstraint(condition=Q(quantity__gt=0), name='cutplansheet_task_quantity_positive'),
        ]
        ordering = ('task_id',)


class SheetStockEvent(models.Model):
    TYPES = [
        ('receive', 'ثبت ورود'), ('adjust', 'اصلاح موجودی'), ('deactivate', 'غیرفعال‌سازی'),
        ('reactivate', 'فعال‌سازی مجدد'), ('reserve', 'رزرو برای برش'), ('release', 'آزادسازی رزرو'),
        ('consume', 'مصرف در برش'), ('offcut', 'ایجاد پرت'), ('untouched', 'ورق مصرف‌نشده'), ('scrap', 'اسقاط'),
    ]
    stock = models.ForeignKey(SheetStock, null=True, blank=True, on_delete=models.SET_NULL, related_name='events')
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='cut_stock_events')
    plan = models.ForeignKey(CutPlan, null=True, blank=True, on_delete=models.SET_NULL, related_name='stock_events')
    event_type = models.CharField(max_length=12, choices=TYPES)
    quantity_delta = models.IntegerField(default=0)
    width = models.DecimalField(verbose_name='عرض (میلی‌متر)', **MM_FIELD)
    height = models.DecimalField(verbose_name='طول (میلی‌متر)', **MM_FIELD)
    is_offcut = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [models.Index(fields=('material', 'created_at'))]
        verbose_name = 'رویداد موجودی ورق'
        verbose_name_plural = 'تاریخچه موجودی ورق'
