from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('inventory', '0018_remove_stockmovement_handout_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='dailymaterialqueue',
            name='return_settled',
            field=models.BooleanField(
                default=False,
                help_text='یعنی انباردار وضعیت ماده را مشخص کرده است، حتی اگر مقدار برگشتی صفر باشد.',
                verbose_name='وضعیت برگشت بررسی شد',
            ),
        ),
    ]
