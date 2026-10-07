"""
اعمال قید یکتا روی `slug`.

حالا هر ردیف یک اسلاغ یونیکدِ متفاوت دارد (مهاجرت ۰۰۲۲)، پس ساخت ایندکس
یکتا امن است. این کار را حتماً جدا از افزودن ستون انجام می‌دهیم، وگرنه
روی داده‌ی قدیمی شکست می‌خورد.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0022_backfill_storefront_slugs'),
    ]

    operations = [
        migrations.AlterField(
            model_name='product',
            name='slug',
            field=models.SlugField(
                allow_unicode=True, blank=True, max_length=220, unique=True,
                verbose_name='اسلاگ',
            ),
        ),
        migrations.AlterField(
            model_name='productcategory',
            name='slug',
            field=models.SlugField(
                allow_unicode=True, blank=True, max_length=120, unique=True,
                verbose_name='اسلاگ',
            ),
        ),
    ]
