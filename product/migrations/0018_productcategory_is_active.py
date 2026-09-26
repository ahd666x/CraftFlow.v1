from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('product', '0017_product_is_active'),
    ]
    operations = [
        migrations.AddField(
            model_name='productcategory',
            name='is_active',
            field=models.BooleanField(default=True, verbose_name="فعال"),
        ),
    ]