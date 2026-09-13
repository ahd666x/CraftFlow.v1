# product/migrations/0010_defect_by_order_and_colorpart.py
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('product', '0009_create_warehouse_group')]
    operations = [
        migrations.AlterField(
            model_name='productiondefect', name='task',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                     related_name='defects', to='product.productiontask',
                                     verbose_name='مرحله/تسک (اختیاری)'),
        ),
        migrations.AddField(
            model_name='productiondefect', name='color_part',
            field=models.CharField(blank=True, max_length=20, verbose_name='بخش رنگی',
                choices=[('بدنه', 'بدنه'), ('درب', 'درب'), ('دستگیره', 'دستگیره'),
                         ('پایه', 'پایه'), ('صفحه', 'صفحه'), ('رینگ', 'رینگ')]),
        ),
    ]