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
