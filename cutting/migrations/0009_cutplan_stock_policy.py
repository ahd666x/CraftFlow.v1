from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('cutting', '0008_cutplansheet_planned_area')]
    operations = [
        migrations.AddField(
            model_name='cutplan',
            name='stock_policy',
            field=models.CharField(
                choices=[('full_first', 'ابتدا ورق کامل، سپس پرت قابل استفاده'), ('offcuts_first', 'ابتدا پرت قابل استفاده، سپس ورق کامل')],
                default='offcuts_first', max_length=20, verbose_name='سیاست مصرف موجودی',
            ),
        ),
    ]
