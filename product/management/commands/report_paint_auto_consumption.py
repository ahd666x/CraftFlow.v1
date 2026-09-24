from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import MaterialIssue, StockMovement
from product.models import ProductionTask


class Command(BaseCommand):
    help = 'گزارش مصرف خودکار نقاشی و (اختیاری) حذف موارد پوشش‌یافته'

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete-covered',
            action='store_true',
            help='حذف موارد covered (پوشش‌یافته) داخل یک تراکنش. پیش‌فرض فقط گزارش است.',
        )

    def handle(self, *args, **options):
        delete_covered = options.get('delete_covered', False)

        movements = list(
            StockMovement.objects.filter(
                movement_type='consumption',
                note__startswith='مصرف خودکار نقاشی - تسک #',
                reference_task__isnull=False,
            ).select_related('raw_material', 'reference_task').order_by('id')
        )

        covered, uncovered = [], []
        for movement in movements:
            task = movement.reference_task
            if task is None:
                uncovered.append((movement, None))
                continue
            has_issue = MaterialIssue.objects.filter(
                order_item=task.order_item,
                color_part=task.color_part,
                raw_material=movement.raw_material,
                status__in=['issued', 'partial'],
                purpose='production',
            ).exists()
            if has_issue:
                covered.append((movement, task))
            else:
                uncovered.append((movement, task))

        total_qty_covered = sum(m.quantity for m, _ in covered)
        total_qty_uncovered = sum(m.quantity for m, _ in uncovered)

        self.stdout.write(f'تعداد کل حرکات مصرف خودکار نقاشی: {len(movements)}')
        self.stdout.write(f'تعداد covered (پوشش‌یافته): {len(covered)} — جمع مقدار: {total_qty_covered}')
        self.stdout.write(f'تعداد uncovered ( بدون پوشش): {len(uncovered)} — جمع مقدار: {total_qty_uncovered}')

        if uncovered:
            self.stdout.write(self.style.WARNING('موارد بدون پوشش (حذف آن‌ها موجودی را غلط می‌کند):'))
            for movement, task in uncovered:
                self.stdout.write(
                    f'  #{movement.id} ماده={movement.raw_material} مقدار={movement.quantity} '
                    f'تسک={task.id if task else "-"}'
                )

        if delete_covered:
            if not covered:
                self.stdout.write('هیچ مورد coveredی برای حذف وجود ندارد.')
                return
            with transaction.atomic():
                ids = [m.id for m, _ in covered]
                deleted, _ = StockMovement.objects.filter(pk__in=ids).delete()
            self.stdout.write(self.style.SUCCESS(f'{deleted} رکورد covered حذف شد.'))
        else:
            self.stdout.write('حالت گزارش: هیچ چیزی حذف نشد (برای حذف --delete-covered را بزنید).')