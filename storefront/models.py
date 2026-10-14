"""
مدل‌های اختصاصی لایه‌ی فروشگاه.

قاعده‌ی مهم این ماژول: هیچ‌چیز اینجا «کپی» از مدل‌های `product` نیست.
`Product`، `ProductCategory`، `Part`، `BOM`، `Color` و کل چرخه‌ی تولید
متعلق به `product` باقی می‌مانند و از این ماژول به آن‌ها ارجاع می‌دهیم.
مدل‌هایی که در `product` وجود ندارند (نظرات، آدرس، مرجوعی، مقایسه)
اینجا تعریف می‌شوند.
"""
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from storefront.base import TimeStampedModel
from storefront.colors import build_palette, hex_to_rgb, rgb_str

hex_validator = RegexValidator(r'^#[0-9a-fA-F]{6}$', 'رنگ باید به شکل #RRGGBB باشد.')


class HeroSlide(TimeStampedModel):
    """
    اسلاید هیرو برای اسلایدر تمام‌عرض صفحه‌ی خانه.
    قابل مدیریت از ادمین برای کنترل اسلایدها بدون تغییر کد.
    """
    title = models.CharField(max_length=120, verbose_name='تیتر')
    subtitle = models.CharField(max_length=200, blank=True, verbose_name='زیرتیتر')
    image = models.ImageField(upload_to='hero/', verbose_name='تصویر اسلاید')
    accent_image = models.ImageField(upload_to='hero/', blank=True, null=True, verbose_name='تصویر تأکید (PNG بدون پس‌زمینه)')
    button_text = models.CharField(max_length=40, default='مشاهده محصولات', verbose_name='متن دکمه')
    link_url = models.CharField(max_length=200, blank=True, verbose_name='لینک دکمه (خالی = صفحه محصولات)')
    order = models.PositiveIntegerField(default=0, verbose_name='ترتیب نمایش')
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    class Meta:
        verbose_name = 'اسلاید هیرو'
        verbose_name_plural = 'اسلایدهای هیرو'
        ordering = ['order', 'id']

    def __str__(self):
        return self.title


class ProductReview(TimeStampedModel):
    product = models.ForeignKey(
        'product.Product', on_delete=models.CASCADE, related_name='reviews',
        verbose_name="محصول",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='product_reviews', verbose_name="کاربر",
    )
    rating = models.PositiveSmallIntegerField(
        choices=[(i, i) for i in range(1, 6)], verbose_name="امتیاز",
    )
    comment = models.TextField(blank=True, verbose_name="نظر")
    image = models.ImageField(upload_to='reviews/', blank=True, null=True, verbose_name="تصویر نظر")
    is_active = models.BooleanField(default=True, verbose_name="فعال")

    class Meta:
        verbose_name = "نظر محصول"
        verbose_name_plural = "نظرات محصول"
        unique_together = ['product', 'user']
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.product.name} ({self.rating})"


class ComparisonList(TimeStampedModel):
    """لیست مقایسه‌ی محصولات، دامنه‌اش نشست کاربر است."""
    session_key = models.CharField(max_length=40, db_index=True, verbose_name="کلید جلسه")
    products = models.ManyToManyField(
        'product.Product', related_name='comparisons', verbose_name="محصولات مقایسه",
    )
    is_active = models.BooleanField(default=True, verbose_name="فعال")

    class Meta:
        verbose_name = "لیست مقایسه"
        verbose_name_plural = "لیست‌های مقایسه"

    def __str__(self):
        return f"مقایسه محصولات - {self.session_key}"


class Address(TimeStampedModel):
    """دفتر آدرس کاربر."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='store_addresses', verbose_name="کاربر",
    )
    title = models.CharField(max_length=50, verbose_name="عنوان آدرس")
    recipient = models.CharField(max_length=100, verbose_name="گیرنده")
    province = models.CharField(max_length=100, blank=True, verbose_name="استان")
    city = models.CharField(max_length=100, blank=True, verbose_name="شهر")
    postal_code = models.CharField(max_length=10, blank=True, verbose_name="کد پستی")
    address = models.TextField(verbose_name="آدرس کامل")
    is_default = models.BooleanField(default=False, verbose_name="آدرس پیش‌فرض")
    is_active = models.BooleanField(default=True, verbose_name="فعال")

    class Meta:
        verbose_name = "آدرس"
        verbose_name_plural = "آدرس‌ها"
        ordering = ['-is_default', '-created_at']

    def __str__(self):
        return f"{self.title} - {self.city}"


class ReturnRequest(TimeStampedModel):
    """درخواست مرجوعی برای یک آیتم سفارش."""
    STATUS_CHOICES = [
        ('pending', 'در انتظار بررسی'),
        ('approved', 'تایید شده'),
        ('rejected', 'رد شده'),
        ('refunded', 'بازپرداخت شده'),
    ]

    order_item = models.ForeignKey(
        'product.OrderItem', on_delete=models.CASCADE,
        related_name='return_requests', verbose_name="آیتم سفارش",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='return_requests', verbose_name="کاربر",
    )
    reason = models.TextField(verbose_name="دلیل مرجوعی")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="وضعیت")
    admin_note = models.TextField(blank=True, verbose_name="یادداشت مدیر")
    processed_at = models.DateTimeField(null=True, blank=True, verbose_name="تاریخ پردازش")
    is_active = models.BooleanField(default=True, verbose_name="فعال")

    class Meta:
        verbose_name = "درخواست مرجوعی"
        verbose_name_plural = "درخواست‌های مرجوعی"
        ordering = ['-created_at']

    def __str__(self):
        return f"مرجوعی #{self.pk}"

    def approve(self, admin_note=''):
        from django.utils import timezone
        self.status = 'approved'
        self.admin_note = admin_note
        self.processed_at = timezone.now()
        self.save()

    def reject(self, admin_note=''):
        from django.utils import timezone
        self.status = 'rejected'
        self.admin_note = admin_note
        self.processed_at = timezone.now()
        self.save()

    def refund(self):
        from django.utils import timezone
        self.status = 'refunded'
        self.processed_at = timezone.now()
        self.save()


class SiteSettings(TimeStampedModel):
    """
    تنظیمات سایت فروشگاه - لوگو، عکس کاور و متنها.

    یک رکورد تنظیمات دارد (Singleton) تا ادمین بدون نیاز به دسترسی
    مستقیم به دیتابیس بتواند لوگو و عکس کاور را عوض کند.
    """
    singleton_enforcer = models.CharField(max_length=1, default='a', editable=False, unique=True)
    logo = models.ImageField(upload_to='site/', blank=True, null=True, verbose_name='لوگو')
    cover_image = models.ImageField(upload_to='site/', blank=True, null=True, verbose_name='عکس کاور')
    site_name = models.CharField(max_length=120, default='فروشگاه', verbose_name='نام سایت')
    hero_title = models.CharField(max_length=200, blank=True, verbose_name='عنوان اصلی')
    hero_subtitle = models.TextField(blank=True, verbose_name='زیرعنوان اصلی')
    footer_text = models.TextField(blank=True, verbose_name='متن فوتر')
    is_maintenance = models.BooleanField(default=False, verbose_name='در حالت تعمیرات')

    # ارسال و تحویل
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=0, default=0, verbose_name='هزینه ارسال (تومان)')
    free_shipping_threshold = models.DecimalField(max_digits=10, decimal_places=0, default=0, verbose_name='آستانه ارسال رایگان (تومان)')
    tax_rate = models.DecimalField(max_digits=4, decimal_places=2, default=0, verbose_name='نرخ مالیات (مثلاً ۰.۰۹ برای ۹٪)')
    production_days_min = models.PositiveIntegerField(default=3, verbose_name='حداقل روز تولید')
    production_days_max = models.PositiveIntegerField(default=7, verbose_name='حداکثر روز تولید')
    shipping_days_min = models.PositiveIntegerField(default=2, verbose_name='حداقل روز ارسال')
    shipping_days_max = models.PositiveIntegerField(default=5, verbose_name='حداکثر روز ارسال')

    # نشان‌های اعتماد
    enamad_logo = models.ImageField(upload_to='site/', blank=True, null=True, verbose_name='لوگو اینماد')
    enamad_url = models.URLField(blank=True, verbose_name='لینک اینماد')
    zarinpal_logo = models.ImageField(upload_to='site/', blank=True, null=True, verbose_name='لوگو زرین‌پال')
    zarinpal_url = models.URLField(blank=True, verbose_name='لینک زرین‌پال')
    warranty_years = models.PositiveIntegerField(default=0, verbose_name='سال‌های گارانتی (۰ = نمایش داده نشود)')

    # شبکه‌های اجتماعی
    instagram_url = models.URLField(blank=True, verbose_name='لینک اینستاگرام')
    telegram_url = models.URLField(blank=True, verbose_name='لینک تلگرام')
    twitter_url = models.URLField(blank=True, verbose_name='لینک توییتر')
    aparat_url = models.URLField(blank=True, verbose_name='لینک آپارات')

    # برند و PWA
    short_name = models.CharField(max_length=30, blank=True, verbose_name='نام کوتاه (برای آیکن نصب روی گوشی)')
    short_description = models.CharField(max_length=200, blank=True, verbose_name='توضیح کوتاه سایت')
    favicon = models.ImageField(upload_to='site/', blank=True, null=True, verbose_name='فاوآیکن')
    pwa_icon = models.ImageField(upload_to='site/', blank=True, null=True, verbose_name='آیکن نصب (PNG مربع ۵۱۲×۵۱۲)')

    # رنگ‌ها
    primary_color = models.CharField(max_length=7, default='#6b543d', validators=[hex_validator], verbose_name='رنگ اصلی')
    secondary_color = models.CharField(max_length=7, default='#ddb050', validators=[hex_validator], verbose_name='رنگ فرعی')
    background_color = models.CharField(max_length=7, default='#faf7f2', validators=[hex_validator], verbose_name='رنگ پس‌زمینه')

    # اطلاعات تماس
    contact_phone = models.CharField(max_length=40, blank=True, verbose_name='تلفن تماس')
    contact_email = models.EmailField(blank=True, verbose_name='ایمیل')
    contact_address = models.TextField(blank=True, verbose_name='آدرس')
    working_hours = models.CharField(max_length=120, blank=True, verbose_name='ساعت کاری')
    about_text = models.TextField(blank=True, verbose_name='متن صفحه درباره ما')

    class Meta:
        verbose_name = 'تنظیمات سایت'
        verbose_name_plural = 'تنظیمات سایت'

    def __str__(self):
        return self.site_name

    @classmethod
    def get_settings(cls):
        from django.db import transaction
        with transaction.atomic():
            obj, _ = cls.objects.select_for_update().get_or_create(
                singleton_enforcer='a',
                defaults={'site_name': 'فروشگاه'}
            )
        return obj

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Invalidate cache when settings are updated
        from django.core.cache import cache
        cache.delete('site_settings')

    @property
    def estimated_delivery_min(self):
        return self.production_days_min + self.shipping_days_min

    @property
    def estimated_delivery_max(self):
        return self.production_days_max + self.shipping_days_max

    @property
    def tax_percent(self):
        pct = self.tax_rate * 100
        return int(pct) if pct == int(pct) else float(pct)

    @property
    def theme_css(self):
        lines = []
        for name, color in (('primary', self.primary_color), ('secondary', self.secondary_color)):
            for shade, rgb in build_palette(color).items():
                lines.append(f'--{name}-{shade}:{rgb};')
        lines.append(f'--cream:{rgb_str(hex_to_rgb(self.background_color))};')
        return ''.join(lines)