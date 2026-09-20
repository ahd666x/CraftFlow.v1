from django.core.management.base import BaseCommand
from django.db import transaction

from product.models import Customer, Order, ProductionTask

CUSTOM_ORDER_NUMBER = 'دلخواه'
CUSTOM_CUSTOMER_NAME = 'کار متفرقه'


class Command(BaseCommand):
    help = 'جدا کردن کارت‌های دلخواه نقاشی از سفارش‌های ساختگی (order=None) و حذف آن سفارش‌ها'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='فقط گزارش، بدون تغییر')

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        dummy_ids = list(
            Order.objects
            .filter(number=CUSTOM_ORDER_NUMBER, items__isnull=True)
            .exclude(tasks__custom_title='')
            .distinct()
            .values_list('id', flat=True)
        )
        if not dummy_ids:
            self.stdout.write(self.style.SUCCESS('سفارش ساختگی‌ای برای پاک‌سازی وجود ندارد.'))
            return

        tasks_count = ProductionTask.objects.filter(order_id__in=dummy_ids).count()
        self.stdout.write(f'سفارش ساختگی: {len(dummy_ids)} | کارت‌های قابل جداسازی: {tasks_count}')

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry-run: هیچ تغییری اعمال نشد.'))
            return

        with transaction.atomic():
            ProductionTask.objects.filter(order_id__in=dummy_ids).update(order=None)
            deleted, _ = Order.objects.filter(pk__in=dummy_ids).delete()
            Customer.objects.filter(name=CUSTOM_CUSTOMER_NAME, order__isnull=True).delete()

        self.stdout.write(self.style.SUCCESS(
            f'انجام شد: {tasks_count} کارت جدا شد، {len(dummy_ids)} سفارش ساختگی حذف شد ({deleted} رکورد).'
        ))
