"""
حذف کامل Engine A و اعمال ساختار Engine B.

ترتیب عملیات عمداً دستی چیده شده و همان ترتیبی است که Django خودش ساخته بود،
با یک تفاوت مهم:

    اول ستون‌های Engine A از جدول ``StockMovement`` برداشته می‌شوند، بعد خودِ
    مدل‌های Engine A حذف می‌شوند.

روی SQLite هر ``RemoveField`` جدول را از نو می‌سازد، و اگر مدلی که
``unique_together``/قید دارد ابتدا از آن قید کم شود، بازسازی جدول با وضعیت
نیمه‌ویراستهٔ مدل شکست می‌خورد. چون آن مدل‌ها قرار است کلاً حذف شوند، اصلاً
``RemoveField`` رویشان لازم نیست.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0010_dailymaterialclosing'),
        ('product', '0018_productcategory_is_active'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ---- ۱) ستون‌های Engine A از دفتر گردش انبار برداشته شوند ----
        # این جدول زنده می‌ماند، پس باید پیش از حذف مدل‌های مقصد پاک شود.
        migrations.RemoveField(
            model_name='stockmovement',
            name='supplier',
        ),
        migrations.RemoveField(
            model_name='stockmovement',
            name='fulfilled_issue',
        ),
        migrations.RemoveField(
            model_name='stockmovement',
            name='catalog_raw_material',
        ),
        migrations.RemoveField(
            model_name='stockmovement',
            name='reference_color_part',
        ),

        # ---- ۲) خودِ مدل‌های Engine A حذف شوند ----
        migrations.DeleteModel(
            name='CustodyConsumption',
        ),
        migrations.DeleteModel(
            name='MaterialCustody',
        ),
        migrations.DeleteModel(
            name='MaterialCustodyReturn',
        ),
        migrations.DeleteModel(
            name='MaterialHandover',
        ),
        migrations.DeleteModel(
            name='MaterialHandoverLine',
        ),
        migrations.DeleteModel(
            name='MaterialIssue',
        ),
        migrations.DeleteModel(
            name='MaterialLeftover',
        ),
        migrations.DeleteModel(
            name='PurchaseOrder',
        ),
        migrations.DeleteModel(
            name='PurchaseOrderItem',
        ),
        migrations.DeleteModel(
            name='Supplier',
        ),

        # ---- ۳) ساختار صف روزانه و دفتر گردش ----
        migrations.AlterModelOptions(
            name='dailymaterialqueue',
            options={
                'ordering': ['work_date', 'worker', 'raw_material'],
                'verbose_name': 'ردیف صف مواد روزانه',
                'verbose_name_plural': 'صف\u200cهای مواد روزانه',
            },
        ),
        migrations.AlterModelOptions(
            name='dailymaterialqueuesource',
            options={
                'ordering': ['queue', 'kind', 'id'],
                'verbose_name': 'منبع صف روزانه',
                'verbose_name_plural': 'منابع صف\u200cهای روزانه',
            },
        ),
        migrations.AlterUniqueTogether(
            name='dailymaterialqueuesource',
            unique_together=set(),
        ),
        migrations.AddField(
            model_name='dailymaterialqueuesource',
            name='defect',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='daily_queue_sources',
                to='product.productiondefect', verbose_name='خرابی',
            ),
        ),
        migrations.AddField(
            model_name='dailymaterialqueuesource',
            name='kind',
            field=models.CharField(
                choices=[
                    ('painting', 'ایستگاه نقاشی'),
                    ('station', 'سایر ایستگاه\u200cها'),
                    ('rework', 'جبران خرابی'),
                ],
                default='painting', max_length=20, verbose_name='نوع منبع',
            ),
        ),
        migrations.AddField(
            model_name='stockmovement',
            name='daily_queue',
            field=models.ForeignKey(
                blank=True,
                help_text='حرکت\u200cهای تحویل و بازگشت به این ردیف صف وصل می\u200cشوند.',
                null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='movements', to='inventory.dailymaterialqueue',
                verbose_name='ردیف صف روزانه',
            ),
        ),
        migrations.AlterField(
            model_name='dailymaterialqueue',
            name='has_plan_conflict',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'وقتی برنامهٔ تولید بعد از تحویل/بازگشت تغییر کند، موجودی و '
                    'تاریخچهٔ واقعی دست\u200cنخورده می\u200cماند و فقط این پرچم فعال می\u200cشود.'
                ),
                verbose_name='تعارض برنامه با تراکنش',
            ),
        ),
        migrations.AlterField(
            model_name='dailymaterialqueue',
            name='planned_quantity',
            field=models.DecimalField(
                decimal_places=2, default=0,
                help_text='نیازی که از برنامهٔ تولید گرفته شده است.',
                max_digits=12, verbose_name='نیاز برنامه\u200cریزی\u200cشده',
            ),
        ),
        migrations.AlterField(
            model_name='dailymaterialqueuesource',
            name='painting_stage',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='daily_queue_sources',
                to='product.paintingstage', verbose_name='مرحله نقاشی',
            ),
        ),
        migrations.AlterField(
            model_name='dailymaterialqueuesource',
            name='production_task',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='daily_queue_sources',
                to='product.productiontask', verbose_name='تسک تولید',
            ),
        ),
        migrations.AlterField(
            model_name='dailymaterialqueuesource',
            name='queue',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='sources', to='inventory.dailymaterialqueue',
                verbose_name='ردیف صف',
            ),
        ),
        migrations.AlterField(
            model_name='stockmovement',
            name='movement_type',
            field=models.CharField(
                choices=[
                    ('purchase', 'خرید/ورود'),
                    ('consumption', 'مصرف (تحویل به تولید)'),
                    ('return', 'مرجوعی از تولید'),
                    ('adjustment', 'اصلاحیه (افزایش)'),
                    ('adjust_out', 'اصلاحیه (کاهش)'),
                ],
                max_length=20, verbose_name='نوع',
            ),
        ),
        migrations.AlterField(
            model_name='stockmovement',
            name='reference_order_item',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='material_movements',
                to='product.orderitem', verbose_name='آیتم سفارش',
            ),
        ),
        migrations.AlterField(
            model_name='stockmovement',
            name='reference_task',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='material_movements',
                to='product.productiontask', verbose_name='تسک تولید',
            ),
        ),
        migrations.AddIndex(
            model_name='stockmovement',
            index=models.Index(
                fields=['raw_material', 'movement_type'],
                name='inventory_s_raw_mat_a6ec3d_idx',
            ),
        ),
    ]