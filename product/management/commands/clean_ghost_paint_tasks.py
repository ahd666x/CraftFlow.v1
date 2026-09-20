# product/management/commands/clean_ghost_paint_tasks.py

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from product.models import ProductionTask
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'پاک‌سازی تسک‌های نقاشی شبح (بدون order_item و/یا painting_stage)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='فقط نمایش تعداد تسک‌ها بدون حذف واقعی',
        )
        parser.add_argument(
            '--include-done',
            action='store_true',
            help='شامل تسک‌های انجام‌شده (done) نیز بشود (پیش‌فرض فقط pending/waiting)',
        )
        parser.add_argument(
            '--fix-null',
            action='store_true',
            help='به‌جای حذف، تسک‌های دارای order_item اما بدون painting_stage را با stage پیش‌فرض اصلاح کند',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        include_done = options.get('include_done', False)
        fix_null = options.get('fix_null', False)

        # ========== کوئری پایه: تسک‌های نقاشی که فاقد order_item یا painting_stage هستند ==========
        ghost_tasks = ProductionTask.objects.filter(
            station_name='paint',
            custom_title='',          # کارت‌های دلخواه شبح نیستند
        ).filter(
            Q(order_item__isnull=True) | Q(painting_stage__isnull=True)
        )

        if not include_done:
            ghost_tasks = ghost_tasks.exclude(status='done')

        count = ghost_tasks.count()
        self.stdout.write(f"تعداد تسک‌های شبح یافت‌شده: {count}")

        if count == 0:
            self.stdout.write(self.style.SUCCESS("✅ هیچ تسک شبحی یافت نشد."))
            return

        # نمایش نمونه‌ای از تسک‌ها
        sample = ghost_tasks[:5]
        self.stdout.write("نمونه‌ای از تسک‌ها:")
        for task in sample:
            self.stdout.write(
                f"  - ID: {task.id}, سفارش: {task.order_id}, "
                f"وضعیت: {task.status}, order_item: {task.order_item_id}, "
                f"painting_stage: {task.painting_stage_id}"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("⚠️ حالت Dry-run: هیچ تغییری اعمال نشد."))
            return

        # ========== حالت Fix: اصلاح تسک‌های دارای order_item بدون painting_stage ==========
        if fix_null:
            with transaction.atomic():
                # پیدا کردن تسک‌هایی که order_item دارند ولی painting_stage ندارند
                fixable = ghost_tasks.filter(
                    order_item__isnull=False,
                    painting_stage__isnull=True
                )
                fixable_count = fixable.count()
                if fixable_count:
                    # از اولین PaintingStage موجود استفاده می‌کنیم (یا می‌توانید منطق دیگری پیاده کنید)
                    from product.models import PaintingStage
                    default_stage = PaintingStage.objects.first()
                    if default_stage:
                        updated = fixable.update(painting_stage=default_stage)
                        self.stdout.write(
                            self.style.SUCCESS(f"✅ {updated} تسک با تنظیم painting_stage پیش‌فرض اصلاح شدند.")
                        )
                    else:
                        self.stdout.write(
                            self.style.WARNING("⚠️ هیچ PaintingStage ای در سیستم وجود ندارد. تسک‌ها قابل اصلاح نیستند.")
                        )
                else:
                    self.stdout.write("هیچ تسکی برای اصلاح یافت نشد.")

                # تسک‌های باقی‌مانده (بدون order_item) را حذف می‌کنیم
                remaining = ghost_tasks.filter(order_item__isnull=True)
                remaining_count = remaining.count()
                if remaining_count:
                    deleted, _ = remaining.delete()
                    self.stdout.write(
                        self.style.SUCCESS(f"✅ {deleted} تسک بدون order_item حذف شدند.")
                    )

        # ========== حالت عادی: حذف همه تسک‌های شبح ==========
        else:
            with transaction.atomic():
                deleted, _ = ghost_tasks.delete()
                self.stdout.write(self.style.SUCCESS(f"✅ {deleted} تسک شبح حذف شدند."))

        # ========== Invalidating کش‌ها ==========
        try:
            from product.utils import invalidate_caches
            invalidate_caches()
            self.stdout.write("✅ کش‌ها پاک شدند.")
        except ImportError:
            self.stdout.write(self.style.WARNING("⚠️ تابع invalidate_caches یافت نشد، کش‌ها دستی پاک شوند."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ خطا در پاک کردن کش‌ها: {e}"))