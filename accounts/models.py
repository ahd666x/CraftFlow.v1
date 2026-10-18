"""
کاربران فروشگاه.

تکراری‌سازی نیست: در این پروژه `AUTH_USER_MODEL` تغییر نکرده و از
`django.contrib.auth.User` استفاده می‌شود. بنابراین این ماژول هیچ مدل
کاربری جدیدی ندارد و فقط منطق تجاریِ ورود/ ثبت‌نام/ پروفایل را فراهم می‌کند.
"""
from django.conf import settings
from django.db import models

from storefront.base import TimeStampedModel


class UserProfile(TimeStampedModel):
    """
    پروفایل تکمیلی کاربر (تماس‌ها و ترجیحات).

    به‌جای تغییر `AUTH_USER_MODEL`، اطلاعات تکمیلی را در این مدل
    یک‌تایی نگه می‌داریم. این روش امن‌تر است چون کل چرخه‌ی تولید
    (`product`) روی `django.contrib.auth.User` ساخته شده و هیچ اشاره‌ای
    به مدل کاربری سفارشی ندارد.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='store_profile', verbose_name="کاربر",
    )
    phone = models.CharField(max_length=15, blank=True, verbose_name="شماره موبایل")
    bio = models.TextField(blank=True, verbose_name="درباره")

    class Meta:
        verbose_name = "پروفایل کاربر"
        verbose_name_plural = "پروفایل‌های کاربران"

    def __str__(self):
        return f"پروفایل {self.user.username}"