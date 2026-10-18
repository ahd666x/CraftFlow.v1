"""
افزودن ستون‌های `slug` (بدون قید یکتا).

ترتیب عمدی است و به همین دلیل در سه فایل شکسته شده:

    0020  افزودن بقیه‌ی ستون‌های فروشگاه
    0021  افزودن ستون slug بدون unique  ← این فایل
    0022  پر کردن اسلاگ رکوردهای موجود
    0023  اعمال unique روی slug

اگر `unique=True` را همراه با افزودن ستون می‌آوردیم، ساخت ایندکس روی
همه‌ی رکوردهای قدیمی با مقدار `''` شکست می‌خورد (`UNIQUE constraint failed`).
پس اول ستون را بدون یکتایی می‌سازیم، داده را پر می‌کنیم، بعد یکتا می‌کنیم.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0020_order_address_order_discount_amount_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='slug',
            field=models.SlugField(
                allow_unicode=True, blank=True, default='', max_length=220,
                verbose_name='اسلاگ',
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='productcategory',
            name='slug',
            field=models.SlugField(
                allow_unicode=True, blank=True, default='', max_length=120,
                verbose_name='اسلاگ',
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='productcategory',
            name='image',
            field=models.ImageField(
                blank=True, null=True, upload_to='categories/',
                verbose_name='تصویر دسته',
            ),
        ),
    ]
