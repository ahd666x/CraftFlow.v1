from django.db import models
from django.contrib.auth.models import User
from django.db.models import Sum, Case, When, Value, DecimalField, F


class Supplier(models.Model):
    name = models.CharField(max_length=150, verbose_name="نام تامین‌کننده")
    phone = models.CharField(max_length=20, blank=True, verbose_name="تلفن")
    address = models.TextField(blank=True, verbose_name="آدرس")
    is_active = models.BooleanField(default=True, verbose_name="فعال")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ثبت")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "تامین‌کننده"
        verbose_name_plural = "تامین‌کنندگان"
        ordering = ['name']


class RawMaterialCategory(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name="نام دسته")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "دسته مواد اولیه"
        verbose_name_plural = "دسته‌های مواد اولیه"
        ordering = ['name']


class RawMaterial(models.Model):
    UNIT_CHOICES = [
        ('kg', 'کیلوگرم'),
        ('lit', 'لیتر'),
        ('pcs', 'عدد'),
        ('m', 'متر'),
    ]

    category = models.ForeignKey(RawMaterialCategory, on_delete=models.PROTECT, related_name='materials', verbose_name="دسته")
    name = models.CharField(max_length=150, verbose_name="نام ماده اولیه")
    code = models.CharField(max_length=50, blank=True, verbose_name="کد")
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, verbose_name="واحد")
    min_stock_alert = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="حداقل موجودی هشدار")
    is_active = models.BooleanField(default=True, verbose_name="فعال")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ثبت")

    @property
    def current_stock(self):
        agg = self.movements.aggregate(
            total=Sum(
                Case(
                    When(movement_type='consumption', then=-F('quantity')),
                    default=F('quantity'),
                    output_field=DecimalField()
                )
            )
        )
        return agg['total'] or 0

    @property
    def stock_status(self):
        stock = self.current_stock
        if stock <= 0:
            return 'danger'
        elif stock <= self.min_stock_alert:
            return 'warning'
        return 'success'

    def __str__(self):
        return f"{self.name} ({self.get_unit_display()})"

    class Meta:
        verbose_name = "ماده اولیه"
        verbose_name_plural = "مواد اولیه"
        ordering = ['category', 'name']
        unique_together = ['category', 'name']


class StockMovement(models.Model):
    MOVEMENT_TYPES = [
        ('purchase', 'خرید/ورود'),
        ('consumption', 'مصرف'),
        ('adjustment', 'اصلاحیه'),
        ('return', 'مرجوعی'),
    ]

    raw_material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name='movements', verbose_name="ماده اولیه")
    catalog_raw_material = models.ForeignKey(
        RawMaterial,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='catalog_consumption_movements',
        verbose_name="ماده اولیه کاتالوگ",
        help_text="برای مصرف نقاشی، ماده کاتالوگ اصلی را مستقل از ماده واقعی رنگ نگه می‌دارد.",
    )
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES, verbose_name="نوع حرکت")
    quantity = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="مقدار")
    unit_price = models.DecimalField(max_digits=12, decimal_places=0, null=True, blank=True, verbose_name="قیمت واحد (ریال)")
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="تامین‌کننده")
    reference_task = models.ForeignKey('product.ProductionTask', null=True, blank=True, on_delete=models.SET_NULL, verbose_name="وظیفه تولید مرتبط")
    note = models.CharField(max_length=255, blank=True, verbose_name="یادداشت")
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, verbose_name="ثبت‌کننده")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ثبت")

    def __str__(self):
        return f"{self.get_movement_type_display()} - {self.raw_material.name} ({self.quantity})"

    class Meta:
        verbose_name = "حرکت انبار"
        verbose_name_plural = "حرکات انبار"
        ordering = ['-created_at']


class MaterialIssue(models.Model):
    """A traceable request and hand-over of material from warehouse to production."""
    STATUS_CHOICES = [
        ('requested', 'در انتظار تحویل'),
        ('partial', 'تحویل ناقص'),
        ('issued', 'تحویل شده'),
        ('cancelled', 'لغو شده'),
    ]
    PURPOSE_CHOICES = [
        ('production', 'برنامه تولید'),
        ('rework', 'جبران خرابی / ساخت مجدد'),
    ]

    task = models.ForeignKey('product.ProductionTask', null=True, blank=True,
                             on_delete=models.SET_NULL, related_name='material_issues', verbose_name='تسک تولید')
    defect = models.ForeignKey('product.ProductionDefect', null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='material_issues', verbose_name='گزارش خرابی')
    raw_material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT,
                                     related_name='issues', verbose_name='ماده اولیه')
    requested_quantity = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='مقدار مورد نیاز')
    issued_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='مقدار تحویل شده')
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, default='production', verbose_name='علت درخواست')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='requested', verbose_name='وضعیت')
    note = models.CharField(max_length=255, blank=True, verbose_name='یادداشت')
    requested_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='requested_material_issues', verbose_name='درخواست کننده')
    issued_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='issued_material_issues', verbose_name='تحویل دهنده')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان درخواست')
    issued_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان تحویل')

    class Meta:
        verbose_name = 'درخواست تحویل مواد'
        verbose_name_plural = 'درخواست‌های تحویل مواد'
        ordering = ['status', '-created_at']

    def __str__(self):
        return f'{self.raw_material} - {self.requested_quantity} ({self.get_status_display()})'


class PurchaseOrder(models.Model):
    STATUS_CHOICES = [
        ('draft', 'پیش‌نویس'),
        ('ordered', 'سفارش‌شده'),
        ('received', 'دریافت‌شده'),
    ]

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, verbose_name="تامین‌کننده")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ایجاد")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name="وضعیت")
    note = models.TextField(blank=True, verbose_name="یادداشت")
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, verbose_name="ثبت‌کننده")

    def __str__(self):
        return f"PO-{self.id}: {self.supplier.name} ({self.get_status_display()})"

    class Meta:
        verbose_name = "سفارش خرید"
        verbose_name_plural = "سفارشات خرید"
        ordering = ['-created_at']


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items', verbose_name="سفارش خرید")
    raw_material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, verbose_name="ماده اولیه")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="مقدار")
    unit_price = models.DecimalField(max_digits=12, decimal_places=0, verbose_name="قیمت واحد (ریال)")
    received_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="مقدار دریافت‌شده")

    @property
    def line_total(self):
        return self.quantity * self.unit_price

    def __str__(self):
        return f"{self.raw_material.name} x {self.quantity}"

    class Meta:
        verbose_name = "آیتم سفارش خرید"
        verbose_name_plural = "آیتم‌های سفارش خرید"
        ordering = ['purchase_order', 'id']
