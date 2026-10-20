"""دستورالعمل‌های تولید — دانش ایستگاه‌ها به تفکیک محصول و قطعه.

هر دستورالعمل به یک محصول (یا در سطح جزئی‌تر، یک قطعه) و یک ایستگاه وصل
است و می‌تواند «شرط نمایش» روی متریال و رنگ داشته باشد؛ موقع گردآوری
دستور تولید برای یک سفارش واقعی، فقط دستورالعمل‌هایی که شرط‌شان با
متریال/رنگ همان سفارش بخواند بالا می‌آیند. مثال واقعی کارگاه: «صفحه
شش‌ضلعی: سه ضلع نوار روکش طبیعی + ضلع پشت نوار PVC؛ اگر متریال ام‌دی‌اف
خام باشد اصلاً نوار نمی‌خورد» — این یعنی دو دستورالعمل برای ایستگاه
نوارکاری، یکی بدون شرط و یکی با شرط متریال خام.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from product.models import STATION_CHOICES, ColorCode, Material, Part, Product


class WorkInstructionQuerySet(models.QuerySet):
    """کوئری‌ست کمکی برای فیلتر «applicable» دستورالعمل‌ها."""

    def active(self):
        return self.filter(active=True)

    def applicable_to(self, material_ids=None, color_codes=None, station=None):
        """فیلتر بر اساس واقعیت سفارش.

        - ``station`` داده شود → دستورالعمل‌های همان ایستگاه + دستورالعمل‌های
          «عمومی» (کد ``general``) برگردانده می‌شوند.
        - ``material_ids`` شناسه‌های متریال قطعاتِ واقعی سفارش است؛ دستورالعملی
          که شرط متریال دارد فقط وقتی می‌آید که تقاطع داشته باشد.
        - ``color_codes`` کدهای رنگ واقعی سفارش است (بدون ``nan``)؛ شرط رنگ
          روی کد رنگ مقایسه می‌شود.
        - شرط‌ها با هم AND می‌شوند؛ دستورالعمل بدون شرط همیشه applicable است.
        """
        qs = self
        if station:
            qs = qs.filter(station__in=[station, self.model.GENERAL])
        material_ids = {m for m in (material_ids or []) if m}
        color_codes = {c for c in (color_codes or []) if c}

        cond = models.Q(condition_material__isnull=True)
        if material_ids:
            cond |= models.Q(condition_material_id__in=material_ids)
        cond_color = models.Q(condition_color__isnull=True)
        if color_codes:
            cond_color |= models.Q(condition_color__code__in=color_codes)
        return qs.filter(cond & cond_color)


class WorkInstruction(models.Model):
    """یک بند دانش تولید: مرحله، نکته، هشدار، چک‌لیست یا مشخصات فنی."""

    GENERAL = 'general'

    KIND_STEP = 'step'
    KIND_NOTE = 'note'
    KIND_WARNING = 'warning'
    KIND_CHECKLIST = 'checklist'
    KIND_SPEC = 'spec'
    KIND_CHOICES = [
        (KIND_STEP, 'مرحله اجرا'),
        (KIND_NOTE, 'نکته'),
        (KIND_WARNING, 'هشدار'),
        (KIND_CHECKLIST, 'چک‌لیست'),
        (KIND_SPEC, 'مشخصات فنی'),
    ]
    # برچسب و کلاس رنگی بوت‌استرپ برای هر نوع (در UI اسکن و سند چاپی)
    KIND_BADGE = {
        KIND_STEP: ('مرحله', 'primary'),
        KIND_NOTE: ('نکته', 'secondary'),
        KIND_WARNING: ('هشدار', 'danger'),
        KIND_CHECKLIST: ('چک‌لیست', 'success'),
        KIND_SPEC: ('مشخصات', 'info'),
    }

    STATION_FILTER_CHOICES = [(GENERAL, 'عمومی (همه ایستگاه‌ها)')] + list(STATION_CHOICES)

    # ترتیب رسمی ایستگاه‌ها در سند «دستور تولید»: عمومی اول، ارسال آخر.
    STATION_ORDER = [GENERAL] + [code for code, _name in STATION_CHOICES if code != 'shipping']

    product = models.ForeignKey(
        Product, null=True, blank=True, on_delete=models.CASCADE,
        related_name='work_instructions', verbose_name='محصول',
        help_text='سطح محصول: برای همه قطعات این محصول در این ایستگاه صادق است',
    )
    part = models.ForeignKey(
        Part, null=True, blank=True, on_delete=models.CASCADE,
        related_name='work_instructions', verbose_name='قطعه',
        help_text='سطح قطعه: فقط برای همین قطعه (مثلاً جهت قرارگیری در سوراخکاری)',
    )
    station = models.CharField(
        max_length=50, choices=STATION_FILTER_CHOICES, default=GENERAL,
        verbose_name='ایستگاه',
    )
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_NOTE, verbose_name='نوع')
    title = models.CharField(max_length=200, verbose_name='عنوان')
    body = models.TextField(
        blank=True, verbose_name='متن',
        help_text='هر خط یک بند جدا نمایش داده می‌شود؛ در چک‌لیست هر خط یک تیک است.',
    )
    condition_material = models.ForeignKey(
        Material, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='work_instructions', verbose_name='شرط متریال',
        help_text='فقط وقتی متریال قطعات سفارش همین باشد نمایش داده شود؛ خالی = همیشه',
    )
    condition_color = models.ForeignKey(
        ColorCode, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='work_instructions', verbose_name='شرط رنگ',
        help_text='فقط وقتی یکی از رنگ‌های سفارش این کد باشد؛ خالی = همیشه',
    )
    sequence = models.PositiveIntegerField(default=0, verbose_name='ترتیب')
    active = models.BooleanField(default=True, verbose_name='فعال')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
        on_delete=models.SET_NULL, related_name='instructions_created', verbose_name='سازنده',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
        on_delete=models.SET_NULL, related_name='instructions_updated', verbose_name='آخرین ویرایشگر',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='ویرایش')

    objects = WorkInstructionQuerySet.as_manager()

    class Meta:
        verbose_name = 'دستورالعمل تولید'
        verbose_name_plural = 'دستورالعمل‌های تولید'
        ordering = ('station', 'sequence', 'id')

    def __str__(self):
        target = self.product.name if self.product_id else (self.part.name if self.part_id else '—')
        return f"[{self.get_station_display()}] {target}: {self.title}"

    def clean(self):
        if not self.product_id and not self.part_id:
            raise ValidationError('دستورالعمل باید به یک محصول یا یک قطعه وصل باشد.')
        if self.product_id and self.part_id:
            raise ValidationError('دستورالعمل فقط یکی از محصول یا قطعه را بپذیرد؛ دانش قطعه‌محور را در سطح قطعه ثبت کنید.')

    @property
    def badge(self):
        """(برچسب فارسی نوع، کلاس بوت‌استرپ) برای UI."""
        return self.KIND_BADGE.get(self.kind, ('نکته', 'secondary'))

    @property
    def condition_labels(self):
        """برچسب خوانای شرط‌ها برای نمایش در ادمین و سند چاپی قالب محصول."""
        labels = []
        if self.condition_material_id:
            labels.append(f"فقط متریال: {self.condition_material.name}")
        if self.condition_color_id:
            labels.append(f"فقط رنگ کد: {self.condition_color.code}")
        return labels

    def body_lines(self):
        return [line.strip() for line in (self.body or '').splitlines() if line.strip()]


class WorkInstructionImage(models.Model):
    """عکس کمکی دستورالعمل (مثلاً صفحه شش‌ضلعی با ضلع‌های علامت‌خورده)."""

    instruction = models.ForeignKey(
        WorkInstruction, on_delete=models.CASCADE, related_name='images', verbose_name='دستورالعمل',
    )
    image = models.ImageField(upload_to='instruction_images/', verbose_name='تصویر')
    caption = models.CharField(max_length=200, blank=True, verbose_name='توضیح تصویر')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='ترتیب')

    class Meta:
        verbose_name = 'تصویر دستورالعمل'
        verbose_name_plural = 'تصاویر دستورالعمل'
        ordering = ('sort_order', 'id')

    def __str__(self):
        return f"تصویر {self.sort_order} برای «{self.instruction.title}»"


class CncMachineSetup(models.Model):
    """راه‌اندازی ثابت ماشین CNC — بخش خودکار ایستگاه CNC در سیستم دستورالعمل‌ها.

    این مدل «سخت‌افزار» را توصیف می‌کند نه محصول را: ابزار هر پارکینگ، روند
    آفست‌زدن، مختصات مبدأ برنامه روی میز و نقاط مجاز/غیرمجاز میخ‌کوبی برای
    فیکس کردن ورق. این داده‌ها به‌طور خودکار به دستورالعمل‌های ایستگاه CNC
    صفحه اسکن، کارت راهنمای اپراتور در /cnc/ و سند دستور تولید اضافه می‌شوند.

    دستگاه مختصات: ``origin_x/origin_y`` مختصات مبدأ برنامه (G54 X0Y0) روی
    میز است؛ هر دو دستگاه (میز و برنامه) با همان جهت فرض می‌شوند — X به راست
    و Y به سمت دور از اپراتور. نقاط فیکس با مختصات میز ثبت می‌شوند و موقع
    ارزیابی به مختصات برنامه برمی‌گردند.
    """

    name = models.CharField(max_length=120, unique=True, verbose_name='نام ماشین')
    table_width = models.PositiveIntegerField(verbose_name='عرض میز X (mm)')
    table_height = models.PositiveIntegerField(verbose_name='طول میز Y (mm)')
    origin_x = models.FloatField(
        verbose_name='مبدأ برنامه X روی میز (mm)',
        help_text='مبدأ G54 (نقطه‌ی صفر برنامه) روی محور X میز؛ اپراتور قطعه را روی همین نقطه سِت می‌کند.',
    )
    origin_y = models.FloatField(
        verbose_name='مبدأ برنامه Y روی میز (mm)',
        help_text='مبدأ G54 (نقطه‌ی صفر برنامه) روی محور Y میز.',
    )
    safety_margin = models.PositiveIntegerField(
        default=10, verbose_name='حاشیه ایمن برش (mm)',
        help_text='فاصله‌ی امن دور مسیر برش که میخ نباید واردش شود (شعاع فرز + فاصله‌ی احتیاط).',
    )
    offset_procedure = models.TextField(
        blank=True, verbose_name='روند آفست‌زدن',
        help_text='هر خط یک بند: مثلاً «بعد از فیکس، دیس تاج را روی سطح بالای ورق بگذارید» / «Z آفست را روی -16.5 تنظیم کنید».',
    )
    notes = models.TextField(
        blank=True, verbose_name='توضیحات عمومی',
        help_text='هر خط یک بند؛ مثلاً «ورق را با لبه‌ی پشتی به ریل توقف بچسبانید».',
    )
    active = models.BooleanField(default=True, verbose_name='فعال')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='ویرایش')

    class Meta:
        verbose_name = 'راه‌اندازی ماشین CNC'
        verbose_name_plural = 'راه‌اندازی ماشین CNC'

    def __str__(self):
        return self.name

    def clean(self):
        if self.origin_x < 0 or self.origin_y < 0:
            raise ValidationError('مبدأ برنامه روی میز نمی‌تواند منفی باشد.')
        if self.origin_x > self.table_width or self.origin_y > self.table_height:
            raise ValidationError('مبدأ برنامه باید داخل محدوده‌ی میز باشد.')

    @property
    def tool_slots_map(self):
        """دیکشنری شماره پارکینگ → ردیف ابزار (برای سریع‌سازی در سریال‌سازی)."""
        return {row.slot: row for row in self.tool_slots.all()}


class CncToolSlot(models.Model):
    """یک پارکینگ ابزار (T#): چه فرزی بسته شود و برای چه کاری."""

    CUTTER_FLAT = 'flat'
    CUTTER_COMPRESSION = 'compression'
    CUTTER_VBIT = 'vbit'
    CUTTER_DRILL = 'drill'
    CUTTER_BALL = 'ball'
    CUTTER_OTHER = 'other'
    CUTTER_CHOICES = [
        (CUTTER_FLAT, 'فرز تخت'),
        (CUTTER_COMPRESSION, 'فرز فشاری (کمپرشن)'),
        (CUTTER_VBIT, 'فرز V'),
        (CUTTER_DRILL, 'مته'),
        (CUTTER_BALL, 'فرز گِرد (بال)'),
        (CUTTER_OTHER, 'سایر'),
    ]

    setup = models.ForeignKey(
        CncMachineSetup, on_delete=models.CASCADE, related_name='tool_slots', verbose_name='ماشین',
    )
    slot = models.PositiveIntegerField(verbose_name='شماره پارکینگ (T)', help_text='شماره ابزار در فایل CNC (T1، T2 و ...).')
    cutter_type = models.CharField(max_length=20, choices=CUTTER_CHOICES, default=CUTTER_FLAT, verbose_name='نوع فرز')
    diameter = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, verbose_name='قطر (mm)')
    description = models.CharField(max_length=200, blank=True, verbose_name='توضیح', help_text='مثلاً «برش متریال ۱۶ میلی» یا «سوراخ‌کاری ۸ میلی».')

    class Meta:
        verbose_name = 'پارکینگ ابزار CNC'
        verbose_name_plural = 'پارکینگ‌های ابزار CNC'
        ordering = ('slot',)
        constraints = [
            models.UniqueConstraint(fields=('setup', 'slot'), name='unique_tool_slot_per_setup'),
        ]

    def __str__(self):
        dia = f" Ø{float(self.diameter):g}" if self.diameter is not None else ''
        return f"T{self.slot}: {self.get_cutter_type_display()}{dia}"

    @property
    def diameter_display(self):
        return f"{float(self.diameter):g}" if self.diameter is not None else '—'


class CncFixturePoint(models.Model):
    """نقطه‌ی فیکس روی میز CNC (میخ/گیره/پین توقف) با مختصات میز.

    ارزیابی ایمنی: نقطه به مختصات برنامه برمی‌گردد (جداشدن از مبدأ) و اگر
    داخل کادر مسیر برش قطعه + حاشیه‌ی امن بیفتد «ممنوع» است؛ بیرون «امن».
    """

    KIND_SCREW = 'screw'
    KIND_CLAMP = 'clamp'
    KIND_STOP = 'stop'
    KIND_OTHER = 'other'
    KIND_CHOICES = [
        (KIND_SCREW, 'میخ/پیچ'),
        (KIND_CLAMP, 'گیره'),
        (KIND_STOP, 'پین توقف / ریل'),
        (KIND_OTHER, 'سایر'),
    ]
    KIND_FA = dict(KIND_CHOICES)

    setup = models.ForeignKey(
        CncMachineSetup, on_delete=models.CASCADE, related_name='fixture_points', verbose_name='ماشین',
    )
    label = models.CharField(max_length=60, verbose_name='برچسب', help_text='مثلاً M1، گیره راست، پین عقب.')
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_SCREW, verbose_name='نوع')
    x = models.FloatField(verbose_name='X روی میز (mm)')
    y = models.FloatField(verbose_name='Y روی میز (mm)')
    radius = models.PositiveIntegerField(
        default=15, verbose_name='شعاع ممنوع (mm)',
        help_text='دور خود نقطه هم این‌قدر فاصله ممنوع است؛ برای سر گیره/سر میخ.',
    )
    active = models.BooleanField(default=True, verbose_name='فعال')

    class Meta:
        verbose_name = 'نقطه فیکس میز CNC'
        verbose_name_plural = 'نقاط فیکس میز CNC'
        ordering = ('label', 'id')

    def __str__(self):
        return f"{self.label} ({self.get_kind_display()})"
