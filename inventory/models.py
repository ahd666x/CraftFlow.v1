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
    barcode = models.CharField(max_length=100, blank=True, unique=True, null=True, verbose_name="بارکد")
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, verbose_name="واحد")
    min_stock_alert = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="حداقل موجودی هشدار")
    pack_size = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, blank=True,
        verbose_name="حجم/وزن هر بسته",
        help_text="مثلاً قوطی ۴ لیتری = 4. صفر یعنی بسته‌بندی ثابت ندارد و مقدار دقیق تحویل داده می‌شود.",
    )
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
        verbose_name="ماده اولیه ",
        help_text="",
    )
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES, verbose_name="نوع")
    quantity = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="مقدار")
    unit_price = models.DecimalField(max_digits=12, decimal_places=0, null=True, blank=True, verbose_name="قیمت واحد (ریال)")
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="تامین‌کننده")
    reference_task = models.ForeignKey('product.ProductionTask', null=True, blank=True, on_delete=models.SET_NULL, verbose_name="وظیفه تولید مرتبط")
    reference_order_item = models.ForeignKey(
        'product.OrderItem', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='paint_material_movements', verbose_name='آیتم سفارش (برای نقاشی)'
    )
    reference_color_part = models.CharField(max_length=20, blank=True, verbose_name='بخش رنگی')
    fulfilled_issue = models.ForeignKey(
        'MaterialIssue', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='movements', verbose_name='درخواست مرتبط',
    )
    note = models.CharField(max_length=255, blank=True, verbose_name="یادداشت")
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, verbose_name="ثبت‌کننده")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ثبت")

    def __str__(self):
        return f"{self.get_movement_type_display()} - {self.raw_material.name} ({self.quantity})"

    class Meta:
        verbose_name = "گردش انبار"
        verbose_name_plural = "گردش انبار"
        ordering = ['-created_at']


class MaterialLeftover(models.Model):
    """
    باقی‌ماندهٔ یک ماده اولیه در سالن تولید (مثلاً ۱ لیتر از قوطی ۴ لیتری که ۳ لیترش مصرف شده).

    این مقدار قبلاً از موجودی انبار خارج شده است؛ پس ویرایش دستی آن (مثلاً ریختن/دورریز قوطی)
    روی موجودی انبار اثری ندارد. در تحویل بعدی، ابتدا از همین مقدار کسر می‌شود.
    """
    raw_material = models.OneToOneField(
        RawMaterial, on_delete=models.CASCADE, related_name='leftover', verbose_name='ماده اولیه'
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='مقدار باقی‌مانده در سالن')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین تغییر')

    class Meta:
        verbose_name = 'باقی‌ماندهٔ سالن تولید'
        verbose_name_plural = 'باقی‌ماندهٔ سالن تولید'

    def __str__(self):
        return f'{self.raw_material.name}: {self.quantity}'


class MaterialHandover(models.Model):
    """یک سند تحویل گروهی: انبار‌دار چند درخواست را یک‌جا به یک تحویل‌گیرنده می‌دهد."""
    issued_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='issued_handovers', verbose_name='تحویل‌دهنده (انبار)'
    )
    received_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='received_handovers', verbose_name='تحویل‌گیرنده'
    )
    note = models.CharField(max_length=255, blank=True, verbose_name='یادداشت')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان تحویل')

    class Meta:
        verbose_name = 'سند تحویل مواد'
        verbose_name_plural = 'اسناد تحویل مواد'
        ordering = ['-created_at']

    def __str__(self):
        return f'سند تحویل {self.pk}'


class MaterialHandoverLine(models.Model):
    """خلاصهٔ هر ماده اولیه در یک سند تحویل (برای ردیابی قوطی‌ها و باقی‌مانده)."""
    handover = models.ForeignKey(MaterialHandover, on_delete=models.CASCADE, related_name='lines', verbose_name='سند تحویل')
    raw_material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name='handover_lines', verbose_name='ماده اولیه')
    required_quantity = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='جمع مورد نیاز')
    leftover_used = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='کسر از باقی‌ماندهٔ سالن')
    from_stock_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='خروج فیزیکی از انبار')
    pack_size = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name='حجم هر بسته (در زمان تحویل)')
    packs_count = models.PositiveIntegerField(default=0, verbose_name='تعداد بسته/قوطی')
    leftover_before = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='باقی‌مانده قبل از تحویل')
    leftover_after = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='باقی‌مانده بعد از تحویل')

    class Meta:
        verbose_name = 'ردیف سند تحویل'
        verbose_name_plural = 'ردیف‌های سند تحویل'
        ordering = ['handover', 'raw_material__name']

    def __str__(self):
        return f'{self.handover_id} — {self.raw_material.name}'


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
    packaging_unit = models.ForeignKey(
        'product.PackagingUnit', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='material_issues', verbose_name='واحد بسته‌بندی'
    )
    order_item = models.ForeignKey(
        'product.OrderItem', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='material_issues', verbose_name='آیتم سفارش'
    )
    painting_process = models.ForeignKey(
        'product.PaintingProcess', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='material_issues', verbose_name='روند نقاشی'
    )
    color_part = models.CharField(
        max_length=20, blank=True, verbose_name='بخش رنگی'
    )
    raw_material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT,
                                      related_name='issues', verbose_name='ماده اولیه')
    handover = models.ForeignKey(
        'MaterialHandover', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='issues', verbose_name='آخرین سند تحویل'
    )
    requested_quantity = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='مقدار مورد نیاز')
    issued_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='مقدار تحویل شده')
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, default='production', verbose_name='علت درخواست')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='requested', verbose_name='وضعیت')
    note = models.CharField(max_length=255, blank=True, verbose_name='یادداشت')
    requested_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='requested_material_issues', verbose_name='درخواست کننده')
    issued_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='issued_material_issues', verbose_name='تحویل دهنده')
    received_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='received_material_issues', verbose_name='تحویل گیرنده')
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