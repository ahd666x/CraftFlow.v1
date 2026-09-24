# product/management/commands/export_schedule.py

import json
import csv
from datetime import datetime, timedelta
from io import StringIO

import jdatetime
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from product.models import ProductionTask, WorkerProfile, OrderItem, PaintingStage


class Command(BaseCommand):
    help = 'استخراج اطلاعات برنامه‌ریزی نقاشی برای تحلیل'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            type=str,
            help='تاریخ به فرمت YYYY-MM-DD (پیش‌فرض: امروز)'
        )
        parser.add_argument(
            '--days',
            type=int,
            default=1,
            help='تعداد روزهای بعد از تاریخ (پیش‌فرض: 1)'
        )
        parser.add_argument(
            '--format',
            choices=['json', 'csv'],
            default='json',
            help='فرمت خروجی (json یا csv)'
        )
        parser.add_argument(
            '--output',
            type=str,
            help='مسیر فایل خروجی (در صورت عدم وارد شدن، در خط فرمان چاپ می‌شود)'
        )
        parser.add_argument(
            '--include-unscheduled',
            action='store_true',
            help='شامل تسک‌های بدون زمان‌بندی نیز بشود'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='نمایش جزئیات بیشتر'
        )

    def handle(self, *args, **options):
        # ---------- دریافت تاریخ ----------
        date_str = options.get('date')
        if date_str:
            try:
                start_date = jdatetime.date(*map(int, date_str.split('-')))
            except:
                self.stderr.write(self.style.ERROR('فرمت تاریخ نامعتبر. از YYYY-MM-DD استفاده کنید.'))
                return
        else:
            start_date = jdatetime.date.today()

        days = options.get('days', 1)
        end_date = start_date + jdatetime.timedelta(days=days - 1)

        format_type = options.get('format', 'json')
        output_file = options.get('output')
        include_unscheduled = options.get('include_unscheduled', False)
        verbose = options.get('verbose', False)

        self.stdout.write(f"📅 بازه زمانی: {start_date} تا {end_date}")

        # ---------- دریافت داده‌ها ----------
        schedule_data = self._build_schedule_data(
            start_date, end_date, include_unscheduled, verbose
        )

        # ---------- خروجی ----------
        if format_type == 'json':
            output = json.dumps(schedule_data, ensure_ascii=False, indent=2, default=str)
        else:  # csv
            output = self._to_csv(schedule_data)

        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(output)
            self.stdout.write(self.style.SUCCESS(f"✅ فایل ذخیره شد: {output_file}"))
        else:
            self.stdout.write(output)

    def _build_schedule_data(self, start_date, end_date, include_unscheduled, verbose):
        """ساخت دیکشنری کامل اطلاعات برنامه‌ریزی"""
        gregorian_start = start_date.togregorian()
        gregorian_end = end_date.togregorian()

        # ---------- ۱. دریافت کارگران ----------
        workers = WorkerProfile.objects.filter(stage='paint').select_related('user')
        workers_data = []
        for w in workers:
            workers_data.append({
                'id': w.user_id,
                'name': w.user.get_full_name() or w.user.username,
                'skills': w.skills or [],
            })

        # ---------- ۲. دریافت تسک‌های زمان‌بندی‌شده در بازه ----------
        scheduled_tasks = ProductionTask.objects.filter(
            station_name='paint',
            scheduled_start__isnull=False,
            scheduled_start__date__gte=gregorian_start,
            scheduled_start__date__lte=gregorian_end,
        ).select_related(
            'order', 'order_item', 'order_item__product', 'order_item__order__customer',
            'painting_stage', 'assigned_worker', 'part'
        ).order_by('scheduled_start', 'assigned_worker_id')

        # گروه‌بندی بر اساس روز و کارگر
        days_data = {}
        task_list = []

        for task in scheduled_tasks:
            # تاریخ شمسی
            shamsi_date = jdatetime.date.fromgregorian(date=timezone.localtime(task.scheduled_start).date())
            date_key = shamsi_date.strftime('%Y-%m-%d')

            if date_key not in days_data:
                days_data[date_key] = {}

            worker_id = task.assigned_worker_id
            if worker_id not in days_data[date_key]:
                worker_name = task.assigned_worker.get_full_name() or task.assigned_worker.username if task.assigned_worker else 'تخصیص‌نیافته'
                days_data[date_key][worker_id] = {
                    'worker_id': worker_id,
                    'worker_name': worker_name,
                    'tasks': []
                }

            # اطلاعات تسک
            task_info = {
                'id': task.id,
                'start': timezone.localtime(task.scheduled_start).isoformat(),
                'end': timezone.localtime(task.scheduled_end).isoformat() if task.scheduled_end else None,
                'duration_minutes': task.painting_stage.duration_minutes if task.painting_stage else None,
                'drying_minutes': task.painting_stage.drying_time_minutes if task.painting_stage else None,
                'order_id': task.order_id,
                'order_number': task.order.number if task.order else None,
                'item_id': task.order_item_id,
                'product_name': task.order_item.product.name if task.order_item and task.order_item.product else None,
                'customer_name': task.order_item.order.customer.name if task.order_item and task.order_item.order and task.order_item.order.customer else None,
                'color_part': task.color_part,
                'painting_stage_name': task.painting_stage.name if task.painting_stage else None,
                'required_skill': task.painting_stage.required_skill if task.painting_stage else None,
                'quantity': task.quantity,
                'status': task.status,
                'part_name': task.part.name if task.part else None,
            }
            days_data[date_key][worker_id]['tasks'].append(task_info)
            task_list.append(task_info)

        # ---------- ۳. محاسبه شکاف‌های خالی برای هر کارگر در هر روز ----------
        gaps_data = []
        for date_key, workers_dict in days_data.items():
            shamsi_date = jdatetime.datetime.strptime(date_key, '%Y-%m-%d').date()
            gregorian = shamsi_date.togregorian()
            bounds = self._get_day_bounds(gregorian)

            for worker_id, worker_data in workers_dict.items():
                tasks = sorted(worker_data['tasks'], key=lambda x: x['start'])
                gaps = self._calculate_gaps(tasks, bounds)
                if gaps:
                    worker_data['gaps'] = gaps
                    gaps_data.append({
                        'date': date_key,
                        'worker_id': worker_id,
                        'worker_name': worker_data['worker_name'],
                        'gaps': gaps
                    })

        # ---------- ۴. تسک‌های بدون زمان‌بندی (اختیاری) ----------
        unscheduled = []
        if include_unscheduled:
            unscheduled_tasks = ProductionTask.objects.filter(
                station_name='paint',
                scheduled_start__isnull=True,
                status__in=['pending', 'waiting'],
                order_item__isnull=False,
                painting_stage__isnull=False,
            ).select_related('order', 'order_item', 'painting_stage')
            for task in unscheduled_tasks:
                unscheduled.append({
                    'id': task.id,
                    'order_id': task.order_id,
                    'item_id': task.order_item_id,
                    'product_name': task.order_item.product.name if task.order_item else None,
                    'color_part': task.color_part,
                    'painting_stage': task.painting_stage.name if task.painting_stage else None,
                    'duration_minutes': task.painting_stage.duration_minutes if task.painting_stage else None,
                    'quantity': task.quantity,
                    'status': task.status,
                })

        # ---------- ۵. تشخیص تداخلات (تسک‌های همپوشان) ----------
        overlaps = self._find_overlaps(days_data)

        # ---------- ۶. آمار کلی ----------
        stats = {
            'total_scheduled_tasks': len(task_list),
            'total_unscheduled_tasks': len(unscheduled) if include_unscheduled else None,
            'total_workers': len(workers_data),
            'total_gaps': len(gaps_data),
            'total_overlaps': len(overlaps),
            'date_range': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': end_date.strftime('%Y-%m-%d'),
            }
        }

        # ---------- خروجی نهایی ----------
        return {
            'stats': stats,
            'workers': workers_data,
            'schedule': days_data,
            'gaps': gaps_data,
            'overlaps': overlaps,
            'unscheduled_tasks': unscheduled if include_unscheduled else None,
            'all_tasks': task_list,
        }

    def _get_day_bounds(self, gregorian_date):
        """مرزهای یک روز کاری"""
        from datetime import datetime, time
        return {
            'start': timezone.make_aware(datetime.combine(gregorian_date, time(8, 0))),
            'end': timezone.make_aware(datetime.combine(gregorian_date, time(16, 30))),
            'break_start': timezone.make_aware(datetime.combine(gregorian_date, time(12, 30))),
            'break_end': timezone.make_aware(datetime.combine(gregorian_date, time(13, 30))),
        }

    def _calculate_gaps(self, tasks, bounds):
        """محاسبه شکاف‌های خالی بین تسک‌ها"""
        if not tasks:
            return [{
                'start': bounds['start'].isoformat(),
                'end': bounds['end'].isoformat(),
                'duration_minutes': int((bounds['end'] - bounds['start']).total_seconds() / 60)
            }]

        gaps = []
        current = bounds['start']

        for task in tasks:
            start = datetime.fromisoformat(task['start'])
            end = datetime.fromisoformat(task['end']) if task['end'] else start + timedelta(minutes=task.get('duration_minutes', 60))

            if start > current:
                gap_minutes = int((start - current).total_seconds() / 60)
                if gap_minutes > 5:  # فقط شکاف‌های بزرگتر از ۵ دقیقه
                    gaps.append({
                        'start': current.isoformat(),
                        'end': start.isoformat(),
                        'duration_minutes': gap_minutes,
                        'is_lunch': (current < bounds['break_start'] and start > bounds['break_end']),
                    })
            if end > current:
                current = end

        # شکاف تا پایان روز
        if bounds['end'] > current:
            gap_minutes = int((bounds['end'] - current).total_seconds() / 60)
            if gap_minutes > 5:
                gaps.append({
                    'start': current.isoformat(),
                    'end': bounds['end'].isoformat(),
                    'duration_minutes': gap_minutes,
                    'is_lunch': False,
                })

        return gaps

    def _find_overlaps(self, days_data):
        """پیدا کردن تسک‌های همپوشان برای یک کارگر در یک روز"""
        overlaps = []
        for date_key, workers_dict in days_data.items():
            for worker_id, worker_data in workers_dict.items():
                tasks = sorted(worker_data['tasks'], key=lambda x: x['start'])
                for i in range(len(tasks) - 1):
                    current = tasks[i]
                    next_task = tasks[i + 1]
                    if current['end'] and next_task['start']:
                        if datetime.fromisoformat(next_task['start']) < datetime.fromisoformat(current['end']):
                            overlaps.append({
                                'date': date_key,
                                'worker_id': worker_id,
                                'worker_name': worker_data['worker_name'],
                                'task1_id': current['id'],
                                'task1_start': current['start'],
                                'task1_end': current['end'],
                                'task2_id': next_task['id'],
                                'task2_start': next_task['start'],
                                'task2_end': next_task['end'],
                                'overlap_minutes': int((datetime.fromisoformat(current['end']) - datetime.fromisoformat(next_task['start'])).total_seconds() / 60)
                            })
        return overlaps

    def _to_csv(self, data):
        """تبدیل داده‌ها به فرمت CSV"""
        output = StringIO()
        writer = csv.writer(output)

        # هدر
        writer.writerow([
            'تاریخ', 'کارگر', 'شروع', 'پایان', 'مدت(دقیقه)', 'خشک‌شدن(دقیقه)',
            'سفارش', 'محصول', 'مشتری', 'مرحله نقاشی', 'کد رنگ', 'وضعیت'
        ])

        for date_key, workers_dict in data['schedule'].items():
            for worker_id, worker_data in workers_dict.items():
                for task in worker_data['tasks']:
                    writer.writerow([
                        date_key,
                        worker_data['worker_name'],
                        task['start'],
                        task['end'],
                        task['duration_minutes'],
                        task['drying_minutes'],
                        task['order_number'] or task['order_id'],
                        task['product_name'],
                        task['customer_name'],
                        task['painting_stage_name'],
                        task['color_part'],
                        task['status'],
                    ])

        # شکاف‌ها
        writer.writerow([])
        writer.writerow(['=== شکاف‌های خالی ==='])
        writer.writerow(['تاریخ', 'کارگر', 'شروع', 'پایان', 'مدت(دقیقه)', 'ناهار'])
        for gap in data.get('gaps', []):
            for g in gap['gaps']:
                writer.writerow([
                    gap['date'],
                    gap['worker_name'],
                    g['start'],
                    g['end'],
                    g['duration_minutes'],
                    'بله' if g.get('is_lunch') else 'خیر',
                ])

        # تداخلات
        writer.writerow([])
        writer.writerow(['=== تداخلات ==='])
        writer.writerow(['تاریخ', 'کارگر', 'تسک اول', 'تسک دوم', 'مدت تداخل(دقیقه)'])
        for overlap in data.get('overlaps', []):
            writer.writerow([
                overlap['date'],
                overlap['worker_name'],
                f"{overlap['task1_start']} - {overlap['task1_end']}",
                f"{overlap['task2_start']} - {overlap['task2_end']}",
                overlap['overlap_minutes'],
            ])

        return output.getvalue()