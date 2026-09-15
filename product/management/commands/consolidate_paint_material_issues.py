from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q, Sum
from decimal import Decimal
from product.models import ProductionTask
from inventory.models import MaterialIssue
from product.utils import get_painting_material_requirements_for_item_colorpart


class Command(BaseCommand):
    help = 'ادغام درخواست‌های مواد نقاشی قدیمی (per-task) به درخواست‌های جدید (per-item/color_part)'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='فقط پیش‌نمایش، تغییری اعمال نمی‌کند')

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        # 1. همه‌ی MaterialIssue های status in ('requested','partial') که purpose='production'
        #    و task__station_name='paint' هستند را بخوان
        old_issues = MaterialIssue.objects.filter(
            status__in=['requested', 'partial'],
            purpose='production',
            task__station_name='paint',
            task__isnull=False,
        ).select_related(
            'task', 'task__order_item', 'raw_material'
        ).order_by('task__order_item_id', 'task__color_part', 'raw_material_id')

        total = old_issues.count()
        self.stdout.write(f'درخواست‌های قدیمی نقاشی یافت شده: {total}')

        if total == 0:
            self.stdout.write(self.style.SUCCESS('هیچ درخواستی برای ادغام وجود ندارد.'))
            return

        # 2. بر اساس (task__order_item_id, task__color_part, raw_material_id) گروه‌بندی کن
        groups = {}
        for issue in old_issues:
            key = (issue.task.order_item_id, issue.task.color_part or '', issue.raw_material_id)
            if key not in groups:
                groups[key] = []
            groups[key].append(issue)

        self.stdout.write(f'تعداد گروه‌ها: {len(groups)}')

        merged_count = 0
        cancelled_count = 0
        warnings = 0

        with transaction.atomic():
            for (item_id, color_part, raw_material_id), issues_in_group in groups.items():
                if not item_id:
                    continue

                # بررسی issued_quantity > 0 در گروه
                has_issued = any(i.issued_quantity > 0 for i in issues_in_group)
                if has_issued:
                    warnings += 1
                    self.stdout.write(self.style.WARNING(
                        f'گروه item={item_id}, color_part={color_part}, material={raw_material_id}: '
                        f'دارای issued_quantity>0 است، برای بازبینی دستی رد می‌شود.'
                    ))
                    continue

                # مقدار درست را از فرمول محاسبه کن
                # نیاز داریم OrderItem را بگیریم
                from product.models import OrderItem
                item = OrderItem.objects.filter(pk=item_id).select_related('product').first()
                if not item:
                    self.stdout.write(self.style.ERROR(f'آیتم سفارش {item_id} یافت نشد'))
                    continue

                process, requirements = get_painting_material_requirements_for_item_colorpart(item, color_part)
                if not process or not requirements:
                    self.stdout.write(self.style.ERROR(f'فرمول برای item={item_id}, color_part={color_part} یافت نشد'))
                    continue

                # پیدا کردن requirement مربوط به این raw_material
                req = next((r for r in requirements if r.raw_material_id == raw_material_id), None)
                if not req:
                    self.stdout.write(self.style.ERROR(f'ماده {raw_material_id} در فرمول item={item_id} وجود ندارد'))
                    continue

                correct_quantity = Decimal(item.quantity) * req.consumption_per_unit
                if correct_quantity <= 0:
                    continue

                # بررسی اینکه آیا درخواست جدید از قبل وجود دارد
                existing_new = MaterialIssue.objects.filter(
                    status__in=['requested', 'partial', 'issued'],
                    purpose='production',
                    order_item_id=item_id,
                    color_part=color_part,
                    raw_material_id=raw_material_id,
                    task__isnull=True,
                ).first()

                if existing_new:
                    self.stdout.write(
                        f'درخواست جدید برای item={item_id}, color_part={color_part}, material={raw_material_id} '
                        f'از قبل وجود دارد (#{existing_new.id})، درخواست‌های قدیمی لغو می‌شوند.'
                    )
                    for old_issue in issues_in_group:
                        if not dry_run:
                            old_issue.status = 'cancelled'
                            old_issue.note = f'{old_issue.note} — ادغام‌شده در درخواست جدید #{existing_new.id}'
                            old_issue.save(update_fields=['status', 'note'])
                        cancelled_count += len(issues_in_group)
                    continue

                # ایجاد درخواست جدید
                if not dry_run:
                    new_issue = MaterialIssue.objects.create(
                        task=None,
                        order_item=item,
                        color_part=color_part,
                        painting_process=process,
                        raw_material=req.raw_material,
                        requested_quantity=correct_quantity,
                        purpose='production',
                        status='requested',
                        requested_by=issues_in_group[0].requested_by,
                        note=(
                            f'نیاز نقاشی (ادغام‌شده): سفارش {item.order_id} / آیتم {item.id} / '
                            f'{item.product.name} / {color_part} / روند {process.name}'
                        ),
                    )
                    # لغو درخواست‌های قدیمی
                    for old_issue in issues_in_group:
                        old_issue.status = 'cancelled'
                        old_issue.note = f'{old_issue.note} — ادغام‌شده در درخواست جدید #{new_issue.id}'
                        old_issue.save(update_fields=['status', 'note'])
                    merged_count += 1
                    cancelled_count += len(issues_in_group)
                    self.stdout.write(
                        f'ایجاد شد: #{new_issue.id} (مقدار={correct_quantity}) | '
                        f'{len(issues_in_group)} درخواست قدیمی لغو شد'
                    )
                else:
                    self.stdout.write(
                        f'[Dry-run] ایجاد می‌شد: item={item_id}, color_part={color_part}, '
                        f'material={raw_material_id}, مقدار={correct_quantity} | '
                        f'{len(issues_in_group)} درخواست قدیمی لغو می‌شد'
                    )
                    merged_count += 1
                    cancelled_count += len(issues_in_group)

        mode = ' (Dry-run)' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{mode} ادغام انجام شد: {merged_count} درخواست جدید | {cancelled_count} درخواست قدیمی لغو شد | {warnings} هشدار'
        ))