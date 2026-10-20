from django.apps import AppConfig


class InstructionsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'instructions'
    verbose_name = 'دستورالعمل‌های تولید'

    def ready(self):
        # اینلاین دستورالعمل‌ها داخل ادمین محصول؛ بعد از autodiscovery ادمین
        # اجرا می‌شود تا ProductAdmin ثبتشده باشد.
        try:
            from .admin import register_product_inline
            register_product_inline()
        except Exception:  # pragma: no cover — ادمین در بعضی فرمان‌ها لود نمی‌شود
            pass
