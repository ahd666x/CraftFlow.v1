import csv
import json
import sys
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from cnc_resizer.anomaly_report import (
    DEFAULT_MAX_ASPECT_RATIO,
    DEFAULT_MAX_LENGTH_MM,
    find_unusual_parts,
)


class Command(BaseCommand):
    help = 'گزارش قطعات CNC/برش با طول غیرمعمول یا غیرقابل جای‌گذاری روی ورق موجود'

    def add_arguments(self, parser):
        parser.add_argument('--order-id', action='append', dest='order_ids', type=int, help='فقط سفارش مشخص؛ قابل تکرار است')
        parser.add_argument('--task-id', action='append', dest='task_ids', type=int, help='فقط تسک مشخص؛ قابل تکرار است')
        parser.add_argument('--max-length', default=str(DEFAULT_MAX_LENGTH_MM), help='حد هشدار طول بر حسب میلی‌متر؛ پیش‌فرض 3000')
        parser.add_argument('--max-aspect-ratio', default=str(DEFAULT_MAX_ASPECT_RATIO), help='حد نسبت طول به عرض؛ پیش‌فرض 8')
        parser.add_argument('--include-done', action='store_true', help='تسک‌های انجام‌شده را هم بررسی کن')
        parser.add_argument('--format', choices=('text', 'csv', 'json'), default='text')

    def handle(self, *args, **options):
        try:
            max_length = Decimal(str(options['max_length']))
            max_ratio = Decimal(str(options['max_aspect_ratio']))
            if max_length <= 0 or max_ratio <= 1:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise CommandError('max-length باید مثبت و max-aspect-ratio باید بزرگ‌تر از 1 باشد.')

        report = find_unusual_parts(
            order_ids=options.get('order_ids'), task_ids=options.get('task_ids'),
            max_length=max_length, max_aspect_ratio=max_ratio,
            include_done=options['include_done'],
        )
        output_format = options['format']
        if output_format == 'json':
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        elif output_format == 'csv':
            writer = csv.writer(self.stdout)
            writer.writerow(['task_id', 'order_id', 'part_id', 'part_name', 'barcode', 'material', 'length', 'width', 'remaining', 'reasons'])
            for row in report:
                writer.writerow([
                    row['task_id'], row['order_id'], row['part_id'], row['part_name'], row['barcode'],
                    row['material'], row['length'], row['width'], row['quantity_remaining'], ' | '.join(row['reasons']),
                ])
        else:
            if not report:
                self.stdout.write(self.style.SUCCESS('قطعهٔ غیرمعمولی پیدا نشد.'))
                return
            for row in report:
                self.stdout.write(
                    f"تسک #{row['task_id']} | سفارش #{row['order_id']} | {row['part_name']} "
                    f"({row['length']}×{row['width']} mm) | متریال: {row['material']}"
                )
                for reason in row['reasons']:
                    self.stdout.write(f'  - {reason}')
            self.stdout.write(self.style.WARNING(f'تعداد موارد نیازمند بررسی: {len(report)}'))
