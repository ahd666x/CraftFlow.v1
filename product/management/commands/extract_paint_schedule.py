# product/management/commands/extract_paint_schedule.py

from datetime import timedelta
import os

import jdatetime
from django.core.management.base import BaseCommand
from django.utils import timezone

from product.models import ProductionTask, WorkerProfile


class Command(BaseCommand):
    help = 'استخراج برنامه نقاشی به صورت گزارش خوانا'

    def handle(self, *args, **options):
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        report_path = os.path.join(project_root, 'paint_schedule_report.txt')

        tasks = ProductionTask.objects.filter(
            station_name='paint',
            scheduled_start__isnull=False,
        ).select_related(
            'painting_stage',
            'order_item',
            'order',
            'assigned_worker',
        ).order_by('assigned_worker_id', 'scheduled_start')

        grouped = {}
        for task in tasks:
            worker_id = task.assigned_worker_id or 'Unassigned'
            worker_name = (
                task.assigned_worker.get_full_name() or task.assigned_worker.username
                if task.assigned_worker else 'تخصیص‌نیافته'
            )
            shamsi_date = jdatetime.date.fromgregorian(date=timezone.localtime(task.scheduled_start).date())
            date_key = shamsi_date.strftime('%Y-%m-%d')

            if worker_id not in grouped:
                grouped[worker_id] = {
                    'worker_name': worker_name,
                    'dates': {},
                }
            if date_key not in grouped[worker_id]['dates']:
                grouped[worker_id]['dates'][date_key] = []

            grouped[worker_id]['dates'][date_key].append(task)

        lines = []
        lines.append('=' * 90)
        lines.append('📋 گزارش برنامه نقاشی')
        lines.append('=' * 90)
        lines.append('')

        total_tasks = 0
        for worker_id, worker_data in grouped.items():
            lines.append(f'👤 کارگر: {worker_data["worker_name"]} (ID: {worker_id})')
            lines.append('-' * 90)
            for date_key, date_tasks in worker_data['dates'].items():
                lines.append(f'  📅 تاریخ: {date_key}')
                for task in date_tasks:
                    total_tasks += 1
                    start_str = timezone.localtime(task.scheduled_start).strftime('%H:%M') if task.scheduled_start else '---'
                    end_str = timezone.localtime(task.scheduled_end).strftime('%H:%M') if task.scheduled_end else '---'
                    stage_name = task.painting_stage.name if task.painting_stage else '-'
                    order_number = task.order.number if task.order else str(task.order_id)
                    lines.append(
                        f'    🔹 [{task.id}] سفارش:{order_number} | آیتم:{task.order_item_id} | '
                        f'بخش:{task.color_part or "-"} | مرحله:{stage_name} | ترتیب:{task.step_order} | '
                        f'زمان:{start_str}-{end_str} | وضعیت:{task.get_status_display()}'
                    )
                lines.append('')

        lines.append('')
        lines.append('=' * 90)
        lines.append('🛡️ بررسی یکپارچگی (Integrity Check)')
        lines.append('=' * 90)
        lines.append('')

        paint_tasks = ProductionTask.objects.filter(
            station_name='paint',
        ).select_related(
            'painting_stage',
            'order_item',
            'assigned_worker',
        ).order_by('order_item_id', 'color_part', 'step_order')

        groups = {}
        for task in paint_tasks:
            key = (task.order_item_id, task.color_part or '')
            if key not in groups:
                groups[key] = []
            groups[key].append(task)

        violations = []

        for (order_item_id, color_part), tasks_in_group in groups.items():
            tasks_in_group.sort(key=lambda t: t.step_order)

            stage_orders = [t.step_order for t in tasks_in_group if t.painting_stage]
            if len(stage_orders) != len(set(stage_orders)):
                violations.append(
                    f'❌ سفارش‌آیتم {order_item_id} / بخش {color_part}: ترتیب مراحل تکراری است.'
                )

            previous_end = None
            previous_drying = 0
            for i, task in enumerate(tasks_in_group):
                if not task.painting_stage:
                    continue

                if task.scheduled_start and previous_end:
                    earliest_start = previous_end + timedelta(minutes=previous_drying)
                    if task.scheduled_start < earliest_start:
                        violations.append(
                            f'❌ سفارش‌آیتم {order_item_id} / بخش {color_part} | مرحله {task.step_order}: '
                            f'زمان شروع ({timezone.localtime(task.scheduled_start).strftime("%Y-%m-%d %H:%M")}) کمتر از '
                            f'زمان پایان مراحل قبلی + خشک‌شدن است.'
                        )

                if task.scheduled_start and task.scheduled_end:
                    if task.scheduled_start >= task.scheduled_end:
                        violations.append(
                            f'❌ سفارش‌آیتم {order_item_id} / بخش {color_part} | مرحله {task.step_order}: '
                            f'زمان شروع ({timezone.localtime(task.scheduled_start)}) برابر یا بعد از زمان پایان است.'
                        )

                for earlier in tasks_in_group:
                    if earlier.step_order >= task.step_order:
                        continue
                    if earlier.status in ['pending', 'waiting'] and earlier.scheduled_start is None:
                        violations.append(
                            f'❌ سفارش‌آیتم {order_item_id} / بخش {color_part} | مرحله {task.step_order}: '
                            f'مرحله {earlier.step_order} ({earlier.get_status_display()}) زمان‌بندی نشده، '
                            f'اما مرحله {task.step_order} زمان‌بندی شده.'
                        )
                        break

                if task.assigned_worker_id and task.painting_stage:
                    try:
                        profile = WorkerProfile.objects.select_related('user').get(user_id=task.assigned_worker_id)
                        required = task.painting_stage.required_skill
                        if required not in (profile.skills or []):
                            violations.append(
                                f'❌ سفارش‌آیتم {order_item_id} / بخش {color_part} | مرحله {task.step_order}: '
                                f'کارگر ({profile.user.get_full_name() or profile.user.username}) مهارت موردنیاز '
                                f'({required}) را ندارد. مهارت‌های ثبت‌شده: {profile.skills or []}'
                            )
                    except WorkerProfile.DoesNotExist:
                        violations.append(
                            f'❌ سفارش‌آیتم {order_item_id} / بخش {color_part} | مرحله {task.step_order}: '
                            f'پروفایل کارگر برای تسک #{task.id} یافت نشد.'
                        )

                if task.scheduled_start and task.scheduled_end and task.painting_stage:
                    previous_end = task.scheduled_end
                    previous_drying = task.painting_stage.drying_time_minutes

        if violations:
            for v in violations:
                lines.append(v)
        else:
            lines.append('✅ هیچ تخلفی یافت نشد.')

        lines.append('')
        lines.append('=' * 90)
        lines.append('📊 خلاصه')
        lines.append('=' * 90)
        lines.append(f'تعداد کل تسک‌های زمان‌بندی‌شده: {total_tasks}')
        lines.append(f'تعداد کارگران فعال: {len(grouped)}')
        lines.append(f'تعداد تخلفات یافت شده: {len(violations)}')
        lines.append('')

        report_text = '\n'.join(lines)

        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_text)

        self.stdout.write(self.style.SUCCESS(f'Report saved to: {report_path}'))
