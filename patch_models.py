import io

path = 'storefront/models.py'
data = open(path, encoding='utf-8').read()

old = (
    "    def refund(self):\n"
    "        from django.utils import timezone\n"
    "        self.status = 'refunded'\n"
    "        self.processed_at = timezone.now()\n"
    "        self.save()\n"
)

new = (
    "    def refund(self):\n"
    "        from django.utils import timezone\n"
    "        self.status = 'refunded'\n"
    "        self.processed_at = timezone.now()\n"
    "        self.save()\n"
    "\n"
    "\n"
    "class SiteSettings(TimeStampedModel):\n"
    "    \"\"\"\n"
    "    تنظیمات سایت فروشگاه - لوگو، عکس کاور و متنها.\n"
    "\n"
    "    یک رکورد تنظیمات دارد (Singleton) تا ادمین بدون نیاز به دسترسی\n"
    "    مستقیم به دیتابیس بتواند لوگو و عکس کاور را عوض کند.\n"
    "    \"\"\"\n"
    "    logo = models.ImageField(upload_to='site/', blank=True, null=True, verbose_name='لوگو')\n"
    "    cover_image = models.ImageField(upload_to='site/', blank=True, null=True, verbose_name='عکس کاور')\n"
    "    site_name = models.CharField(max_length=120, default='فروشگاه', verbose_name='نام سایت')\n"
    "    hero_title = models.CharField(max_length=200, blank=True, verbose_name='عنوان اصلی')\n"
    "    hero_subtitle = models.TextField(blank=True, verbose_name='زیرعنوان اصلی')\n"
    "    footer_text = models.TextField(blank=True, verbose_name='متن فوتر')\n"
    "    is_maintenance = models.BooleanField(default=False, verbose_name='در حالت تعمیرات')\n"
    "\n"
    "    class Meta:\n"
    "        verbose_name = ' regulation settings سایت'\n"
    "        verbose_name_plural = ' regulation settings سایت'\n"
    "\n"
    "    def __str__(self):\n"
    "        return self.site_name\n"
    "\n"
    "    def save(self, *args, **kwargs):\n"
    "        # حذف رکورد قبلی برای نگه داشتن تنها یک نمونه (singleton)\n"
    "        SiteSettings.objects.exclude(pk=self.pk).delete()\n"
    "        super().save(*args, **kwargs)\n"
    "\n"
    "    @classmethod\n"
    "    def get_settings(cls):\n"
    "        obj = cls.objects.first()\n"
    "        if not obj:\n"
    "            obj = cls.objects.create()\n"
    "        return obj\n"
)

assert old in data, 'old block not found'
data = data.replace(old, new, 1)
open(path, 'w', encoding='utf-8').write(data)
print('patched storefront/models.py')