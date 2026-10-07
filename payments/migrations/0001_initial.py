from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Payment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='تاریخ به‌روزرسانی')),
                ('amount', models.DecimalField(decimal_places=0, max_digits=15, verbose_name='مبلغ')),
                ('status', models.CharField(choices=[('pending', 'در انتظار'), ('success', 'موفق'), ('failed', 'ناموفق'), ('cancelled', 'لغو شده')], default='pending', max_length=20, verbose_name='وضعیت')),
                ('gateway', models.CharField(default='zarinpal', max_length=50, verbose_name='درگاه پرداخت')),
                ('transaction_id', models.CharField(blank=True, max_length=100, verbose_name='شناسه تراکنش')),
                ('authority', models.CharField(blank=True, max_length=100, verbose_name='کد-authority')),
                ('paid_at', models.DateTimeField(blank=True, null=True, verbose_name='تاریخ پرداخت')),
                ('order', models.OneToOneField(on_delete=models.deletion.CASCADE, related_name='payment', to='product.order', verbose_name='سفارش')),
            ],
            options={
                'verbose_name': 'پرداخت',
                'verbose_name_plural': 'پرداخت‌ها',
            },
        ),
        migrations.CreateModel(
            name='Transaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='تاریخ به‌روزرسانی')),
                ('amount', models.DecimalField(decimal_places=0, max_digits=15, verbose_name='مبلغ')),
                ('ref_id', models.CharField(blank=True, max_length=100, verbose_name='شماره مرجع')),
                ('card_pan', models.CharField(blank=True, max_length=20, verbose_name='شماره کارت')),
                ('payment', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='transactions', to='payments.payment', verbose_name='پرداخت')),
            ],
            options={
                'verbose_name': 'traکنش',
                'verbose_name_plural': 'traکنش‌ها',
            },
        ),
    ]