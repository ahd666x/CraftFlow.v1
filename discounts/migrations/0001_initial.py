from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Discount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='تاریخ به‌روزرسانی')),
                ('code', models.CharField(max_length=50, unique=True, verbose_name='کد تخفیف')),
                ('discount_type', models.CharField(choices=[('percent', 'درصدی'), ('fixed', 'ثابت')], default='percent', max_length=10, verbose_name='نوع تخفیف')),
                ('value', models.PositiveIntegerField(verbose_name='مقدار تخفیف')),
                ('max_uses', models.PositiveIntegerField(default=1, verbose_name='حداکثر استفاده')),
                ('used_count', models.PositiveIntegerField(default=0, verbose_name='تعداد استفاده')),
                ('valid_from', models.DateTimeField(verbose_name='اعتبار از')),
                ('valid_until', models.DateTimeField(verbose_name='اعتبار تا')),
                ('is_active', models.BooleanField(default=True, verbose_name='عالی')),
            ],
            options={
                'verbose_name': 'تخفیف',
                'verbose_name_plural': 'تخفیف‌ها',
            },
        ),
    ]