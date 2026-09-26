from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('product', '0016_alter_color_code_and_more'),
    ]
    operations = [
        migrations.AddField(
            model_name='product',
            name='is_active',
            field=models.BooleanField(default=True, verbose_name="فعال"),
        ),
    ]