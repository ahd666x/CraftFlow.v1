from django.core.management.base import BaseCommand
from datetime import datetime, time as dt_time
from django.utils import timezone
from django.db import transaction
from product.models import ProductionTask, ProductionEvent
from product.utils import consume_material_for_task, consume_material_for_paint_task
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


class Command(BaseCommand):
    help = 'پر کردن Historical ProductionEvent و مصرف خودکار مواد برای تسک‌های done قدیمی'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='فقط نمایش تعداد رویدادها بدون ایجاد واقعی',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)

        done_tasks = ProductionTask.objects.filter(status='done', order__isnull=False)
        total = done_tasks.count()
        self.stdout.write(f'تعداد تسک‌های done: {total}')

        existing_event_task_ids = set(
            ProductionEvent.objects.filter(task__in=done_tasks, event_type='done')
            .values_list('task_id', flat=True)
        )
        tasks_without_events = list(done_tasks.exclude(id__in=existing_event_task_ids))
        self.stdout.write(f'تعداد تسک‌های done بدون رویداد: {len(tasks_without_events)}')

        if dry_run:
            self.stdout.write(self.style.WARNING('حالت Dry-run: هیچ تغییری اعمال نشد.'))
            return

        with transaction.atomic():
            events_to_create = []
            for task in tasks_without_events:
                events_to_create.append(ProductionEvent(
                    task=task,
                    order=task.order,
                    order_item=task.order_item,
                    station_name=task.station_name,
                    event_type='done',
                    quantity=task.completed_quantity or task.quantity,
                    old_status='',
                    new_status='done',
                    user=task.scanned_by,
                ))

            if events_to_create:
                created_events = ProductionEvent.objects.bulk_create(events_to_create, batch_size=1000)
                self.stdout.write(self.style.SUCCESS(f'تعداد {len(created_events)} رویداد تولید ایجاد شد.'))

                events_to_update = []
                for event, task in zip(created_events, tasks_without_events):
                    if task.completed_at:
                        gregorian_date = task.completed_at.togregorian() if hasattr(task.completed_at, 'togregorian') else task.completed_at
                        event.created_at = timezone.make_aware(datetime.combine(gregorian_date, dt_time.min))
                    else:
                        event.created_at = timezone.now()
                    events_to_update.append(event)

                if events_to_update:
                    ProductionEvent.objects.bulk_update(events_to_update, ['created_at'], batch_size=1000)
                    self.stdout.write(self.style.SUCCESS('تاریخ رویدادها با تاریخ تکمیل تسک هماهنگ شد.'))
            else:
                self.stdout.write('هیچ رویداد جدیدی لازم نیست.')

            consumed = 0
            skipped = 0
            for task in done_tasks:
                if task.station_name == 'paint':
                    result = consume_material_for_paint_task(task)
                else:
                    result = consume_material_for_task(task)
                if result:
                    consumed += 1
                else:
                    skipped += 1

            self.stdout.write(
                f'مصرف مواد: {consumed} تسک پردازش‌شده (نقاشی و غیرنقاشی)، '
                f'{skipped} از قبل وجود داشت یا قابل مصرف نبود.'
            )
