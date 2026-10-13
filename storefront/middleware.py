"""
Middleware برای مدیریت حالت تعمیرات.
"""
from django.shortcuts import render
from django.conf import settings
from django.core.cache import cache
from storefront.models import SiteSettings


class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # برای درخواست‌های استاتیک، ادمین و احراز هویت، بررسی نکن
        if (request.path.startswith('/static/') or 
            request.path.startswith('/media/') or 
            request.path.startswith('/admin/') or
            request.path.startswith('/accounts/')):
            return self.get_response(request)

        # بررسی حالت تعمیرات (با کش برای جلوگیری از کوئری تکراری)
        try:
            site_settings = cache.get('site_settings')
            if site_settings is None:
                site_settings = SiteSettings.get_settings()
                cache.set('site_settings', site_settings, 300)  # کش 5 دقیقه
            
            if site_settings.is_maintenance:
                # سوپر یوزرها و استاف می‌توانند در حالت تعمیرات هم دسترسی داشته باشند
                if request.user.is_authenticated and (request.user.is_superuser or request.user.is_staff):
                    return self.get_response(request)
                
                # برای بقیه، صفحه تعمیرات نمایش داده شود
                response = render(request, 'storefront/maintenance.html', status=503)
                return response
        except Exception:
            # در صورت خطا در دریافت تنظیمات، به صورت عادی ادامه ده
            pass

        return self.get_response(request)