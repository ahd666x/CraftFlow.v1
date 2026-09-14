from django.core.management.base import BaseCommand
from django.db import transaction
from inventory.models import MaterialIssue, StockMovement


class Command(BaseCommand):
    help = 'لینک کردن StockMovement های قدیمی به MaterialIssue بر اساس تطابق task + raw_material + یادداشت شامل #<issue_id>'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        qs = MaterialIssue.objects.filter(
            status__in=['issued', 'partial'],
            stock_movement__isnull=True,
            task__isnull=False,
        ).select_related('raw_material', 'task')

        total = qs.count()
        self.stdout.write(f'MaterialIssue without links: {total}')

        linked = 0
        ambiguous = 0
        not_found = 0

        with transaction.atomic():
            for issue in qs:
                candidates = StockMovement.objects.filter(
                    movement_type='consumption',
                    reference_task=issue.task,
                    raw_material=issue.raw_material,
                    note__icontains=f'#{issue.id}',
                    fulfilled_issue__isnull=True,
                )
                count = candidates.count()
                if count == 1:
                    movement = candidates.first()
                    if not dry_run:
                        issue.stock_movement = movement
                        issue.save(update_fields=['stock_movement'])
                    linked += 1
                elif count == 0:
                    not_found += 1
                else:
                    ambiguous += 1
                    self.stdout.write(self.style.WARNING(
                        f'  issue #{issue.id}: {count} matching movements found, skipped for manual review'
                    ))

        mode = '(dry-run)' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{mode} linked: {linked} | not found: {not_found} | ambiguous: {ambiguous}'
        ))
