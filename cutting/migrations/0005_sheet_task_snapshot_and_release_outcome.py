from collections import Counter

from django.db import migrations, models
import django.db.models.deletion


def backfill_sheet_task_counts(apps, schema_editor):
    CutPlan = apps.get_model('cutting', 'CutPlan')
    CutPlanSheet = apps.get_model('cutting', 'CutPlanSheet')
    CutPlanTask = apps.get_model('cutting', 'CutPlanTask')
    CutPlanSheetTask = apps.get_model('cutting', 'CutPlanSheetTask')
    alias = schema_editor.connection.alias

    for plan in CutPlan.objects.using(alias).all().iterator():
        valid_sheets = set(CutPlanSheet.objects.using(alias).filter(plan_id=plan.pk).values_list('pk', flat=True))
        valid_tasks = set(CutPlanTask.objects.using(alias).filter(plan_id=plan.pk).values_list('task_id', flat=True))
        counts_by_sheet = {}
        for row in (plan.plan_data or {}).get('rows', []):
            for layout in row.get('sheets', []):
                sheet = layout.get('sheet') or {}
                try:
                    sheet_id = int(sheet['plan_sheet_id'])
                except (KeyError, TypeError, ValueError):
                    continue
                if sheet_id not in valid_sheets:
                    continue
                counts = counts_by_sheet.setdefault(sheet_id, Counter())
                for piece in layout.get('parts', []):
                    try:
                        task_id = int(piece['task_id'])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if task_id not in valid_tasks:
                        continue
                    counts[task_id] += 1

        for sheet_id, counts in counts_by_sheet.items():
            for task_id, quantity in counts.items():
                CutPlanSheetTask.objects.using(alias).get_or_create(
                    plan_sheet_id=sheet_id,
                    task_id=task_id,
                    defaults={'quantity': quantity},
                )


class Migration(migrations.Migration):
    dependencies = [('cutting', '0004_partial_plan_progress')]

    operations = [
        migrations.CreateModel(
            name='CutPlanSheetTask',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity', models.PositiveIntegerField()),
                ('plan_sheet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='task_counts', to='cutting.cutplansheet')),
                ('task', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='cut_plan_sheet_counts', to='product.productiontask')),
            ],
            options={'ordering': ('task_id',)},
        ),
        migrations.AddConstraint(
            model_name='cutplansheettask',
            constraint=models.UniqueConstraint(fields=('plan_sheet', 'task'), name='cutplansheet_task_once'),
        ),
        migrations.AddConstraint(
            model_name='cutplansheettask',
            constraint=models.CheckConstraint(condition=models.Q(('quantity__gt', 0)), name='cutplansheet_task_quantity_positive'),
        ),
        migrations.AlterField(
            model_name='cutplansheet', name='outcome',
            field=models.CharField(choices=[('reserved', 'در انتظار نتیجه'), ('used', 'مصرف شد'), ('untouched', 'استفاده نشد'), ('scrapped', 'غیرقابل استفاده'), ('released', 'رزرو آزاد شد؛ برنامه لغو شد')], default='reserved', max_length=12),
        ),
        migrations.RunPython(backfill_sheet_task_counts, migrations.RunPython.noop),
    ]
