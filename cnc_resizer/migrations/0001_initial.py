from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CncFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("barcode_key", models.CharField(db_index=True, max_length=200, verbose_name="کلید بارکد")),
                ("path", models.CharField(max_length=500, verbose_name="مسیر فایل")),
                ("sha256", models.CharField(db_index=True, max_length=64, verbose_name="SHA-256")),
                ("kind", models.CharField(choices=[("base", "فایل پایه"), ("derived", "فایل مشتق")], default="base", max_length=20)),
                ("params", models.JSONField(blank=True, default=dict, verbose_name="پارامترهای تولید")),
                ("length_before", models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True)),
                ("length_after", models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True)),
                ("resizable", models.BooleanField(default=False, verbose_name="قابل تغییر اندازه")),
                ("validation_error", models.TextField(blank=True, verbose_name="خطای اعتبارسنجی")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_cnc_files", to=settings.AUTH_USER_MODEL)),
                ("source_file", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="derived_files", to="cnc_resizer.cncfile", verbose_name="فایل منبع")),
            ],
            options={
                "verbose_name": "فایل CNC",
                "verbose_name_plural": "فایل‌های CNC",
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="cncfile",
            constraint=models.UniqueConstraint(fields=("barcode_key", "sha256", "kind"), name="unique_cnc_file_revision"),
        ),
    ]
