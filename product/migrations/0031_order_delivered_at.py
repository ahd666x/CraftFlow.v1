from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0030_remove_productbom_cnc_axis_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='delivered_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='تاریخ تحویل'),
        ),
    ]
