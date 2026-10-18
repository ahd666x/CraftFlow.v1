from django.db import migrations, models


def snapshot_old_task_totals(apps, schema_editor):
    CutPlanTask = apps.get_model('cutting', 'CutPlanTask')
    alias = schema_editor.connection.alias
    for row in CutPlanTask.objects.using(alias).all().iterator():
        row.task_quantity = row.quantity
        row.completed_before = 0
        row.save(update_fields=('task_quantity', 'completed_before'))


class Migration(migrations.Migration):
    dependencies = [('cutting', '0003_seed_stock_history')]
    operations = [
        migrations.AddField(
            model_name='cutplantask', name='task_quantity',
            field=models.PositiveIntegerField(default=1, editable=False, verbose_name='کل تعداد تسک هنگام برنامه‌ریزی'),
        ),
        migrations.AddField(
            model_name='cutplantask', name='completed_before',
            field=models.PositiveIntegerField(default=0, editable=False, verbose_name='تعداد تکمیل‌شده پیشین'),
        ),
        migrations.RunPython(snapshot_old_task_totals, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='cutplan', name='status',
            field=models.CharField(choices=[('planned', 'برنامه‌ریزی'), ('cut', 'برش انجام شد'), ('partial', 'نتیجه جزئی؛ ادامه لازم است'), ('cancelled', 'لغو شد')], default='planned', max_length=12),
        ),
    ]
