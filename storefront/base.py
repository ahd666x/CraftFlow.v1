from django.db import models


class TimeStampedModel(models.Model):
    """
    نسخه‌ی محلیِ ``BaseModel`` پروژه‌ی store.

    عمداً نامش را با مبدأ یکسان نگه نداشتیم، چون در CraftFlow مدل‌های
    موجود (`product`/`inventory`) از این الگو استفاده نمی‌کنند و افزودن
    ``is_active`` به آن‌ها یک مهاجرت پرهزینه و بی‌فایده است. این پایه فقط
    برای مدل‌های تازه‌ی فروشگاه است.
    """
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ایجاد")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="تاریخ بروزرسانی")

    class Meta:
        abstract = True
