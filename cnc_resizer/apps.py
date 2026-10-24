from django.apps import AppConfig


class CncResizerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cnc_resizer"
    verbose_name = "CNC length resizer"

    def ready(self):
        # فاز ۴ (P3): سیگنال قالب پارامتریک قطعه → فلگ برنامه‌های ورق برنامه‌ریزی‌شده
        from . import signals  # noqa: F401
