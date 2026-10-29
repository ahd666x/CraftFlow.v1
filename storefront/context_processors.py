"""Context processor اختصاصی فروشگاه.

`site_settings` را در همه‌ی قالب‌های فروشگاه (هدر، فوتر،
صفحه‌ی اصلی و ...) در دسترس می‌گذارد تا نام سایت، لوگو،
عکس کاور و متن‌ها از پنل مدیریت قابل ویرایش باشند.
"""
from django.core.cache import cache

from .models import SiteSettings
from .permissions import can_see_prices as _can_see_prices


def site_settings(request):
    # کش ۶۰ ثانیه‌ای: یکی‌شدن کوئری تنظیمات در همهٔ درخواست‌ها.
    # SiteSettings.save() کش را باطل می‌کند؛ ردیف خالی کش نمی‌شود تا
    # تست‌ها و صفحه‌های قبل از migrate دچار مقدار None نگه‌داشته نشوند.
    settings_obj = cache.get('site_settings')
    if settings_obj is None:
        try:
            settings_obj = SiteSettings.objects.first()
        except Exception:
            # جدول هنوز مایگریت نشده (مثلاً هنگام migrate)
            return {'site_settings': None}
        if settings_obj is not None:
            cache.set('site_settings', settings_obj, 60)
    return {'site_settings': settings_obj}


def is_home(request):
    """چک می‌کند که آیا صفحه جاری home است یا خیر."""
    try:
        return {'is_home': request.resolver_match and request.resolver_match.url_name == 'home'}
    except Exception:
        return {'is_home': False}


def price_visibility(request):
    """آیا کاربر قیمت‌ها را ببیند؟ — برای مخفی کردن قیمت در فروشگاه."""
    user = getattr(request, 'user', None)
    if user is None:
        return {'can_see_prices': False}
    if not hasattr(request, '_can_see_prices'):
        request._can_see_prices = _can_see_prices(user)
    return {'can_see_prices': request._can_see_prices}
