from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cnc_resizer", "0011_cncparttemplate"),
    ]

    operations = [
        migrations.AddField(
            model_name="cncsheetplan",
            name="preflight_report",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.AddField(
            model_name="cncsheetplan",
            name="template_stale",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="cncsheetplan",
            name="template_stale_info",
            field=models.JSONField(blank=True, default=list, editable=False),
        ),
    ]
