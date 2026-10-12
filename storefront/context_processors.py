"""Context processor اختصاصی فروشگاه.

`site_settings` را در همه‌ی قالب‌های فروشگاه (هدر، فوتر،
صفحه‌ی اصلی و ...) در دسترس می‌گذارد تا نام سایت، لوگو،
عکس کاور و متن‌ها از پنل مدیریت قابل ویرایش باشند.
"""
from .models import SiteSettings


def site_settings(request):
    try:
        return {'site_settings': SiteSettings.objects.first()}
    except Exception:
        # جدول هنوز مایگریت نشده (مثلاً هنگام migrate)
        return {'site_settings': None}
