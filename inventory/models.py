from django.db import models
from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Sum, Case, When, Value, DecimalField, F
from django.utils import timezone


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

    #: تنها تعریف «کدام حرکت موجودی را کم می‌کند». هر گزارش و ویویی که علامت
    #: موجودی را حساب می‌کند باید از همین فهرست استفاده کند تا یک حرکت در دو
    #: جا با دو علامت شمرده نشود.
    STOCK_DECREASING_TYPES = ('consumption', 'adjust_out', 'purchase_reversal')

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
                    When(movement_type__in=RawMaterial.STOCK_DECREASING_TYPES,
                         then=-F('quantity')),
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
    """
    دفتر واحد انبار.

    هر تغییر موجودی یک ردیف این جدول است؛ هیچ جای دیگری مستقیم موجودی را
    دستکاری نمی‌کند. سه منبع نوشتن وجود دارد و هر سه از صف روزانه یا اسکن
    دریافت می‌آیند:

        * ``purchase``          — اسکن و دریافت کالا (ورودی)
        * ``consumption``       — تحویل از صف مواد روزانه (تنها راه کسر بابت تولید)
        * ``return``            — بازگشت پایان روز از صف مواد روزانه
        * ``adjustment``        — اصلاح دستی انباردار (افزایش موجودی)
        * ``adjust_out``        — اصلاح دستی انباردار (کاهش موجودی)
        * ``purchase_reversal`` — ابطال دریافت (منفی کردن یک purchase قبلی)
        * ``stock_count``       — اصلاح خودکار از سند شمارش انبار

    ``daily_queue`` ردیف صف را نشان می‌دهد که این حرکت از آن آمده تا مصرف
    قابل ردیابی تا سطح «کارگر و روز» بماند. ``reference_task`` و
    ``reference_order_item`` مسیر دوم را نگه می‌دارند تا معلوم شود این حرکت
    در نهایت برای کدام سفارش بوده است.

    برای ``purchase_reversal`` فیلد ``reverses`` به حرکت اصلی purchase اشاره می‌کند.
    """

    MOVEMENT_TYPES = [
        ('purchase', 'خرید/ورود'),
        ('consumption', 'مصرف (تحویل به تولید)'),
        ('return', 'مرجوعی از تولید'),
        ('adjustment', 'اصلاحیه (افزایش)'),
        ('adjust_out', 'اصلاحیه (کاهش)'),
        ('purchase_reversal', 'ابطال خرید/ورود'),
        ('stock_count', 'اصلاح شمارش انبار'),
    ]

    raw_material = models.ForeignKey(RawMaterial, on_delete=models.PROTECT, related_name='movements', verbose_name="ماده اولیه")
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES, verbose_name="نوع")
    quantity = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="مقدار")
    unit_price = models.DecimalField(max_digits=12, decimal_places=0, null=True, blank=True, verbose_name="قیمت واحد (ریال)")
    reference_task = models.ForeignKey(
        'product.ProductionTask', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='material_movements', verbose_name='تسک تولید',
    )
    reference_order_item = models.ForeignKey(
        'product.OrderItem', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='material_movements', verbose_name='آیتم سفارش',
    )
    daily_queue = models.ForeignKey(
        'DailyMaterialQueue', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='movements', verbose_name='ردیف صف روزانه',
        help_text='حرکت‌های تحویل و بازگشت به این ردیف صف وصل می‌شوند.',
    )
    # برای purchase_reversal: اشاره به حرکت purchase اصلی که ابطال می‌شود
    reverses = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT,
        related_name='reversals', verbose_name='ابطال‌کننده',
        help_text='برای purchase_reversal: حرکت purchase اصلی که ابطال می‌شود.',
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
        indexes = [
            models.Index(fields=['raw_material', 'movement_type']),
        ]


# ============================================================
#  صف تحویل مواد روزانه — تنها مسیر تحویل مواد به تولید
# ============================================================

class DailyMaterialQueue(models.Model):
    """
    یک ردیف «چه کسی، چه روزی، چه مقداری از چه ماده‌ای لازم دارد».

    این تنها جدولی است که انباردار برای تحویل با آن کار می‌کند و از هر سه
    منبع نیاز مشتق می‌شود:

        * ایستگاه نقاشی  — ``ProductionTask(station='paint')`` از برنامهٔ نقاشی
        * سایر ایستگاه‌ها — ``ProductionTask(part.material.raw_material)``
        * جبران خرابی   — ``ProductionDefect`` با مادهٔ جایگزین تعیین‌شده

    چون نیاز از برنامه مشتق می‌شود، انباردار چیزی وارد نمی‌کند و درخواست
    تکراری ساخته نمی‌شود: هر واحد کار فقط یک‌بار شمرده می‌شود.
    """

    STATUS_CHOICES = [
        ('pending', 'در انتظار تحویل'),
        ('partial', 'تحویل ناقص'),
        ('delivered', 'تحویل شده'),
        ('returned', 'برگشت ثبت شده'),
        ('closed', 'پایان روز بسته شد'),
        ('cancelled', 'لغو شده'),
    ]

    work_date = models.DateField(verbose_name="تاریخ کاری")
    worker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='daily_material_queues', verbose_name="کارگر",
    )
    raw_material = models.ForeignKey(
        RawMaterial, on_delete=models.CASCADE,
        related_name='daily_queues', verbose_name="ماده اولیه",
    )
    painting_stage = models.ForeignKey(
        'product.PaintingStage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='daily_queues',
        verbose_name="مرحله نقاشی",
        help_text="مرحله نقاشی که این ردیف صف مربوط به آن است (برای ردیابی تفکیک شده).",
    )
    color_part = models.CharField(
        max_length=50, blank=True, verbose_name="بخش رنگی",
        help_text="بخش رنگی (مثلاً بدنه، درب) برای تفکیک نیازهای هم‌مرحله.",
    )
    planned_quantity = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        verbose_name="نیاز برنامه‌ریزی‌شده",
        help_text="نیازی که از برنامهٔ تولید گرفته شده است.",
    )
    delivered_quantity = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        verbose_name="تحویل شده",
    )
    returned_quantity = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        verbose_name="برگشتی",
    )
    actual_consumption = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        verbose_name="مصرف واقعی",
        help_text="delivered_quantity - returned_quantity",
    )
    excess_consumption = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        verbose_name="مصرف اضافه",
        help_text="max(0, actual_consumption - planned_quantity)",
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending',
        verbose_name="وضعیت",
    )
    note = models.CharField(max_length=255, blank=True, verbose_name="یادداشت")
    has_plan_conflict = models.BooleanField(
        default=False,
        verbose_name="تعارض برنامه با تراکنش",
        help_text=(
            "وقتی برنامهٔ تولید بعد از تحویل/بازگشت تغییر کند، موجودی و "
            "تاریخچهٔ واقعی دست‌نخورده می‌ماند و فقط این پرچم فعال می‌شود."
        ),
    )
    conflict_note = models.CharField(
        max_length=255, blank=True, verbose_name="شرح تعارض",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ایجاد")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="بروزرسانی")

    class Meta:
        verbose_name = "ردیف صف مواد روزانه"
        verbose_name_plural = "صف‌های مواد روزانه"
        ordering = ['work_date', 'worker', 'raw_material', 'painting_stage', 'color_part']
        unique_together = ('work_date', 'worker', 'raw_material', 'painting_stage', 'color_part')
        indexes = [
            models.Index(fields=['work_date', 'worker']),
            models.Index(fields=['work_date', 'status']),
            models.Index(fields=['work_date', 'painting_stage']),
        ]

    def __str__(self):
        return (
            f"{self.work_date} — {self.worker} — "
            f"{self.raw_material.name} ({self.planned_quantity})"
        )

    @property
    def has_transaction(self):
        """آیا تحویل یا بازگشت واقعی برای این صف ثبت شده است؟"""
        return bool(
            (self.delivered_quantity and self.delivered_quantity > 0)
            or (self.returned_quantity and self.returned_quantity > 0)
        )

    def recalculate_consumption(self, commit=False):
        """
        مقادیر مشتق‌شده (مصرف واقعی / مصرف اضافه) را از تحویل و بازگشت
        دوباره حساب می‌کند. planned_quantity و وضعیت انجام تسک دست‌نخورده می‌مانند.
        """
        actual = self.computed_actual_consumption
        excess = self.computed_excess_consumption
        changed = (actual != self.actual_consumption) or (excess != self.excess_consumption)
        self.actual_consumption = actual
        self.excess_consumption = excess
        if commit and changed:
            self.save(update_fields=['actual_consumption', 'excess_consumption', 'updated_at'])
        return changed

    @property
    def computed_actual_consumption(self):
        from decimal import Decimal
        return (self.delivered_quantity or 0) - (self.returned_quantity or 0)

    @property
    def computed_excess_consumption(self):
        from decimal import Decimal
        actual = self.computed_actual_consumption
        planned = self.planned_quantity or 0
        return max(actual - planned, 0) if actual > planned else 0


class DailyMaterialQueueSource(models.Model):
    """
    ردیابی اینکه نیازِ یک ردیف صف از کجا آمده است.

    هر ردیف نشان می‌دهد چه سهمی از ``planned_quantity`` از یک منبع گرفته
    شده. ``kind`` تعیین می‌کند منبع چیست:

        * ``painting`` — تسک ایستگاه نقاشی و مرحلهٔ آن
        * ``station``  — تسک سایر ایستگاه‌ها (نیاز از قطعه/ماده می‌آید)
        * ``rework``   — جبران یک خرابی

    مثال:
        ردیف صف: عباس | رنگ سفید | ۳ کیلو
            ├── نقاشی  — تسک A → ۱ (روند رنگ‌آمیزی سفید، مرحله ۱)
            ├── نقاشی  — تسک B → ۱ (روند رنگ‌آمیزی سفید، مرحله ۲)
            └── جبران  — خرابی #۱۲ → ۱
    """

    KIND_CHOICES = [
        ('painting', 'ایستگاه نقاشی'),
        ('station', 'سایر ایستگاه‌ها'),
        ('rework', 'جبران خرابی'),
        ('carryover', 'انتقال کسری از روز قبل'),
    ]

    queue = models.ForeignKey(
        DailyMaterialQueue, on_delete=models.CASCADE,
        related_name='sources', verbose_name="ردیف صف",
    )
    kind = models.CharField(
        max_length=20, choices=KIND_CHOICES, default='painting',
        verbose_name="نوع منبع",
    )
    production_task = models.ForeignKey(
        'product.ProductionTask', null=True, blank=True, on_delete=models.CASCADE,
        related_name='daily_queue_sources', verbose_name="تسک تولید",
    )
    painting_stage = models.ForeignKey(
        'product.PaintingStage', null=True, blank=True, on_delete=models.CASCADE,
        related_name='daily_queue_sources', verbose_name="مرحله نقاشی",
    )
    defect = models.ForeignKey(
        'product.ProductionDefect', null=True, blank=True, on_delete=models.CASCADE,
        related_name='daily_queue_sources', verbose_name="خرابی",
    )
    raw_material = models.ForeignKey(
        RawMaterial, on_delete=models.CASCADE,
        related_name='daily_queue_sources', verbose_name="ماده اولیه",
    )
    quantity = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        verbose_name="سهم از این منبع",
    )
    carryover_from = models.ForeignKey(
        'DailyMaterialQueue', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='carried_sources', verbose_name="ردیف مبدأ کسری",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ایجاد")

    class Meta:
        verbose_name = "منبع صف روزانه"
        verbose_name_plural = "منابع صف‌های روزانه"
        ordering = ['queue', 'kind', 'id']
        constraints = [
            # Prevent duplicate sources for same queue + identifying fields
            models.UniqueConstraint(
                fields=['queue', 'kind', 'production_task', 'painting_stage', 'defect', 'carryover_from', 'raw_material'],
                name='unique_queue_source_identifiers',
            ),
        ]

    def __str__(self):
        if self.kind == 'rework':
            return f"{self.queue} ← خرابی {self.defect_id} / {self.quantity}"
        return (
            f"{self.queue} ← تسک {self.production_task_id} / "
            f"مرحله {self.painting_stage_id} / {self.quantity}"
        )


class DailyMaterialClosing(models.Model):
    """
    تأیید نهایی روز: «روز بررسی و تأیید شد».

    این مدل فقط یک سند تأیید است و هیچ اثر انباری یا برنامه‌ای ندارد:

        * ``StockMovement`` ایجاد نمی‌کند;
        * مقادیر ``DailyMaterialQueue`` را تغییر نمی‌دهد;
        * فقط ثبت می‌کند که انباردار روز را دیده و تأیید کرده است.

    شرط ثبت (در ``services.confirm_daily_closing`` و نه در این مدل) این است که
    روز «مشکل کنترل‌نشده» نداشته باشد. ``work_date`` یکتا است تا یک روز دوبار
    بسته نشود.
    """

    STATUS_CHOICES = [
        ('confirmed', 'روز بررسی و تأیید شد'),
        ('locked', 'قفل شده — تغییرات ممنوع'),
    ]

    work_date = models.DateField(
        unique=True, verbose_name="تاریخ کاری",
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='confirmed',
        verbose_name="وضعیت",
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='daily_material_closings',
        verbose_name="تأییدکننده",
    )
    closed_at = models.DateTimeField(
        auto_now_add=True, verbose_name="زمان تأیید",
    )
    locked_at = models.DateTimeField(
        null=True, blank=True, verbose_name="زمان قفل",
    )
    locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='daily_material_locks',
        verbose_name="قفل‌کننده",
    )
    note = models.CharField(
        max_length=255, blank=True, verbose_name="یادداشت",
    )

    class Meta:
        verbose_name = "تأیید پایان روز"
        verbose_name_plural = "تأیید پایان روزها"
        ordering = ['-work_date']

    def lock(self, locked_by):
        """قفل کردن روز — پس از این هیچ تحویل/بازگشت/لغو برای این تاریخ مجاز نیست."""
        self.status = 'locked'
        self.locked_at = timezone.now()
        self.locked_by = locked_by
        self.save(update_fields=['status', 'locked_at', 'locked_by', 'updated_at'])

    @classmethod
    def is_locked(cls, date):
        """بررسی اینکه آیا روز قفل شده است."""
        try:
            return cls.objects.get(work_date=date).status == 'locked'
        except cls.DoesNotExist:
            return False

    def __str__(self):
        return f"{self.work_date} — {self.get_status_display()}"


class StockCount(models.Model):
    """
    سند شمارش انبار (Stocktake).

    یک سند شمارش شامل چندین خط (StockCountLine) برای مواد مختلف است.
    در حالت پیش‌نویس (draft) هیچ اثری روی موجودی ندارد.
    پس از تأیید (approve)، برای هر خط که اختلاف دارد، یک StockMovement
    از نوع ``stock_count`` یا ``adjustment``/``adjust_out`` ساخته می‌شود
    که به این سند شمارش ارجاع دارد.
    """

    STATUS_CHOICES = [
        ('draft', 'پیش‌نویس'),
        ('approved', 'تأیید شده'),
    ]

    date = models.DateField(verbose_name="تاریخ شمارش", default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name="وضعیت")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='stock_counts_created', verbose_name="شمارش‌کننده",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='stock_counts_approved', verbose_name="تأییدکننده",
    )
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="زمان تأیید")
    note = models.CharField(max_length=255, blank=True, verbose_name="یادداشت")

    class Meta:
        verbose_name = "سند شمارش انبار"
        verbose_name_plural = "سندهای شمارش انبار"
        ordering = ['-date', '-id']

    def __str__(self):
        return f"شمارش {self.date} — {self.get_status_display()}"

    def approve(self, approved_by):
        """تأیید سند شمارش و ایجاد حرکات اصلاح برای خطوط با اختلاف."""
        if self.status == 'approved':
            raise ValueError('این سند شمارش قبلاً تأیید شده است.')
        from django.db import transaction
        from decimal import Decimal

        with transaction.atomic():
            for line in self.lines.all():
                diff = line.counted_quantity - line.system_quantity
                if diff == 0:
                    continue
                # Determine movement type based on sign of difference
                if diff > 0:
                    movement_type = 'adjustment'
                else:
                    movement_type = 'adjust_out'
                    # Prevent negative stock
                    current_stock = line.raw_material.current_stock
                    if current_stock + diff < 0:
                        raise ValueError(
                            f'موجودی «{line.raw_material.name}» برای اصلاح شمارش کافی نیست. '
                            f'موجودی فعلی: {current_stock}, اصلاحیه: {diff}'
                        )

                StockMovement.objects.create(
                    raw_material=line.raw_material,
                    movement_type=movement_type,
                    quantity=abs(diff),
                    note=f'اصلاح شمارش انبار: {self.date} (سیستم: {line.system_quantity}, شمارش: {line.counted_quantity})',
                    created_by=approved_by,
                )
            self.status = 'approved'
            self.approved_by = approved_by
            self.approved_at = timezone.now()
            self.save(update_fields=['status', 'approved_by', 'approved_at'])

    def can_edit(self):
        return self.status == 'draft'


class StockCountLine(models.Model):
    """
    یک خط در سند شمارش انبار.

    هر خط یک ماده و مقایسه موجودی سیستم با تعداد فیزیکی شمرده شده را نگه می‌دارد.
    """
    stock_count = models.ForeignKey(
        StockCount, on_delete=models.CASCADE, related_name='lines', verbose_name="سند شمارش",
    )
    raw_material = models.ForeignKey(
        RawMaterial, on_delete=models.PROTECT, related_name='stock_count_lines', verbose_name="ماده اولیه",
    )
    system_quantity = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="موجودی سیستم",
        help_text="موجودی محاسبه‌شده توسط سیستم در لحظه شمارش.",
    )
    counted_quantity = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name="تعداد شمرده شده",
    )
    note = models.CharField(max_length=255, blank=True, verbose_name="یادداشت خط")

    class Meta:
        verbose_name = "خط شمارش انبار"
        verbose_name_plural = "خطوط شمارش انبار"
        unique_together = ('stock_count', 'raw_material')
        ordering = ['raw_material__category', 'raw_material__name']

    @property
    def difference(self):
        return self.counted_quantity - self.system_quantity

    def __str__(self):
        return f"{self.raw_material.name}: سیستم {self.system_quantity} / شمارش {self.counted_quantity} (تفاوت {self.difference})"