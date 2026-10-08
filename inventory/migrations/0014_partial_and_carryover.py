import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('inventory', '0013_alter_dailymaterialqueue_options_and_more')]

    operations = [
        migrations.AlterField(
            model_name='dailymaterialqueue', name='status',
            field=models.CharField(
                choices=[('pending', 'در انتظار تحویل'), ('partial', 'تحویل ناقص'),
                         ('delivered', 'تحویل شده'), ('returned', 'برگشت ثبت شده'),
                         ('closed', 'پایان روز بسته شد'), ('cancelled', 'لغو شده')],
                default='pending', max_length=20, verbose_name='وضعیت'),
        ),
        migrations.AlterField(
            model_name='dailymaterialqueuesource', name='kind',
            field=models.CharField(
                choices=[('painting', 'ایستگاه نقاشی'), ('station', 'سایر ایستگاه‌ها'),
                         ('rework', 'جبران خرابی'),
                         ('carryover', 'انتقال کسری از روز قبل')],
                default='painting', max_length=20, verbose_name='نوع منبع'),
        ),
        migrations.AddField(
            model_name='dailymaterialqueuesource', name='carryover_from',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='carried_sources', to='inventory.dailymaterialqueue',
                verbose_name='ردیف مبدأ کسری'),
        ),
    ]
