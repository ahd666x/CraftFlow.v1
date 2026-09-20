from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0008_product_image'),
    ]

    operations = [
        migrations.AddField(
            model_name='color',
            name='hex_code',
            field=models.CharField(blank=True, max_length=7, verbose_name='کد هگز رنگ'),
        ),
        migrations.AddField(
            model_name='color',
            name='material_name',
            field=models.CharField(blank=True, max_length=50, verbose_name='نام متریال پیش\u200cفرض'),
        ),
    ]
