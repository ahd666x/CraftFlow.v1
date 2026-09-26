from django.core.management.base import BaseCommand
from django.db import transaction
from inventory.models import MaterialIssue, StockMovement


class Command(BaseCommand):
    help = 'لینک کردن StockMovement های قدیمی به MaterialIssue بر اساس تطابق task + raw_material + یادداشت شامل #<issue_id>'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        import re

        dry_run = options['dry_run']
        qs = MaterialIssue.objects.filter(
            status__in=['issued', 'partial'], task__isnull=False,
        ).select_related('task', 'raw_material')

        total = qs.count()
        self.stdout.write(f'MaterialIssue (issued/partial, task-based): {total}')

        linked = 0
        with transaction.atomic():
            for issue in qs:
                # مرز انتهایی لازم است تا #5 با #50 اشتباه نشود.
                pattern = rf'#{re.escape(str(issue.id))}(\D|$)'
                candidates = StockMovement.objects.filter(
                    movement_type='consumption', reference_task=issue.task,
                    raw_material=issue.raw_material, fulfilled_issue__isnull=True,
                    note__regex=pattern,
                )
                count = candidates.count()
                if count == 0:
                    continue
                if not dry_run:
                    candidates.update(fulfilled_issue=issue)
                linked += count

        mode = '(dry-run)' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{mode} linked: {linked} حرکت به درخواست‌های موجود وصل شد.'
        ))
