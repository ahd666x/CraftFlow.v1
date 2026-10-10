#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script: plan_paint_item_461.sh
برنامه‌ریزی آیتم نقاشی و بررسی تکامل مواد اولیه در صف انبار

Usage:
    python plan_paint_item_461.sh                       # plan + verify
    python plan_paint_item_461.sh --item-id 461         # specify item
    python plan_paint_item_461.sh --dry-run              # no planning, just verify
"""
import sys
import os
import json
import traceback
from datetime import timedelta
from decimal import Decimal
from argparse import ArgumentParser

sys.stdout.reconfigure(encoding='utf-8')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'selvi.settings')
import django
django.setup()

import jdatetime
from product.models import (
    OrderItem, ProductionTask, PaintingProcess, PaintingStage,
    PaintingMaterialRequirement, PaintingProcessMaterial, Product, Order
)
from inventory.models import DailyMaterialQueue, RawMaterial
from product.utils import (
    create_and_schedule_items_for_date,
    get_item_color_assignments,
    get_painting_process_for_color,
    get_resolved_painting_requirements_for_task,
)
from inventory.services import (
    sync_daily_material_queue_for_dates,
    execute_daily_delivery,
    execute_daily_return,
    HandoverError,
    _work_date as _inv_work_date,
    _q2,
    _physical_for,
)
from inventory.models import DailyMaterialQueue, RawMaterial, StockMovement
from django.contrib.auth.models import User


def parse_args():
    parser = ArgumentParser(description='برنامه‌ریزی و بررسی آیتم نقاشی')
    parser.add_argument('--item-id', type=int, default=461,
                        help='شناسه آیتم سفارشی (پیش‌فرض: 461)')
    parser.add_argument('--dry-run', action='store_true',
                        help='فقط بررسی، برنامه‌ریزی نمی‌کند')
    parser.add_argument('--fix-stages', action='store_true',
                        help='بروزرسانی stage_id در PMR بر اساس کاتالوگ PaintingProcessMaterial')
    return parser.parse_args()


def fix_pmr_stages(item):
    """Sync PaintingMaterialRequirement stage_id to match PaintingProcessMaterial catalog.

    For each PMR entry:
    - If the catalog (PaintingProcessMaterial) has a specific stage for (raw_material, process),
      assign that stage to the PMR.
    - If the catalog has stage_id=NULL, remove the stage from the PMR (NULL = all stages).
    - If no catalog entry exists, leave the PMR unchanged and report it.

    This ensures materials only appear in the warehouse queue at the stages they
    are actually needed.
    """
    from django.db import transaction
    from product.utils import get_painting_process_for_color

    assignments = get_item_color_assignments(item)
    updated = []
    skipped = []

    with transaction.atomic():
        for part_name, color_code in assignments:
            process = get_painting_process_for_color(color_code)
            if not process:
                continue

            reqs = PaintingMaterialRequirement.objects.filter(
                process_id=process.id,
                product_id=item.product_id,
                color_part=part_name,
            )
            for r in reqs:
                catalog = PaintingProcessMaterial.objects.filter(
                    raw_material=r.raw_material,
                    process=process,
                ).first()

                new_stage_id = None
                if catalog and catalog.stage_id is not None:
                    new_stage_id = catalog.stage_id
                elif catalog and catalog.stage_id is None:
                    new_stage_id = None
                else:
                    skipped.append(
                        f'{r.raw_material.name}/{part_name}: '
                        f'بدون کاتالوگ؛ stage روی {r.stage.name if r.stage else "NULL"} نگهداری شد'
                    )
                    continue

                old_stage_name = r.stage.name if r.stage else 'NULL'
                if r.stage_id != new_stage_id:
                    r.stage_id = new_stage_id
                    r.save(update_fields=['stage'])
                    new_stage_name = (
                        PaintingStage.objects.get(id=new_stage_id).name
                        if new_stage_id else 'NULL (تمام مراحل)'
                    )
                    updated.append(
                        f'{r.raw_material.name}/{part_name}: '
                        f'{old_stage_name} -> {new_stage_name}'
                    )
                else:
                    skipped.append(
                        f'{r.raw_material.name}/{part_name}: '
                        f'همخوانی stage ({old_stage_name}) - بدون تغییر'
                    )

    return updated, skipped


def cleanup_existing_tasks(item):
    """Delete existing paint tasks and queue entries for this item before re-planning.

    Order matters: delete DailyMaterialQueue rows FIRST (via the sources
    relationship), because ProductionTask deletion cascades to
    DailyMaterialQueueSource with on_delete=CASCADE — once sources are gone,
    the queue cleanup filter would find nothing.
    """
    from django.db import transaction
    with transaction.atomic():
        # Delete queue rows that link to this item's paint tasks — BEFORE
        # deleting the tasks themselves (source cascade).
        old_queue_ids = list(
            DailyMaterialQueue.objects.filter(
                sources__production_task__order_item=item,
                sources__production_task__station_name='paint',
            ).distinct().values_list('id', flat=True)
        )
        DailyMaterialQueue.objects.filter(
            sources__production_task__order_item=item,
            sources__production_task__station_name='paint',
        ).distinct().delete()

        task_ids = list(ProductionTask.objects.filter(
            order_item=item, station_name='paint'
        ).values_list('id', flat=True))
        ProductionTask.objects.filter(
            order_item=item, station_name='paint'
        ).delete()
        return len(task_ids)


def main():
    args = parse_args()
    ITEM_ID = args.item_id
    dry_run = args.dry_run

    item = OrderItem.objects.get(id=ITEM_ID)
    assignments = get_item_color_assignments(item)

    print('=' * 80)
    print(f'STEP 0: بررسی و اصلاح stage_id در PaintingMaterialRequirement')
    print('=' * 80)
    print(f'آیتم: {item} | محصول: {item.product} | تعداد: {item.quantity}')
    print(f'سفارش: {item.order}')

    print('=' * 80)
    print(f'STEP 0: بررسی و اصلاح stage_id در PaintingMaterialRequirement')
    print('=' * 80)
    print(f'آیتم: {item} | محصول: {item.product} | تعداد: {item.quantity}')
    print(f'سفارش: {item.order}')

    if args.fix_stages:
        print()
        print('در حال همگام‌سازی stage_id PMR با کاتالوگ PaintingProcessMaterial...')
        updated, skipped = fix_pmr_stages(item)
        for u in updated:
            print(f'  اصلاح شد: {u}')
        for s in skipped:
            print(f'  بدون تغییر: {s}')
        print(f'کل: {len(updated)} به‌روزرسانی، {len(skipped)} بدون تغییر')

        if not dry_run:
            print()
            print('در حال پاکسازی تسک‌ها و صف‌های قبلی برای بازسازی...')
            removed = cleanup_existing_tasks(item)
            print(f'  {removed} تسک و ردیف صف حذف شد.')
    else:
        print('برای اصلاح stage_id، از --fix-stages استفاده کنید.')
        fixable = []
        for part_name, color_code in assignments:
            process = get_painting_process_for_color(color_code)
            if not process:
                continue
            reqs = PaintingMaterialRequirement.objects.filter(
                process_id=process.id,
                product_id=item.product_id,
                color_part=part_name,
            )
            for r in reqs:
                catalog = PaintingProcessMaterial.objects.filter(
                    raw_material=r.raw_material, process=process
                ).first()
                if catalog:
                    cat_stage = catalog.stage.name if catalog.stage else 'NULL'
                    pmr_stage = r.stage.name if r.stage else 'NULL'
                    if cat_stage != pmr_stage or pmr_stage != cat_stage:
                        fixable.append(
                            f'{r.raw_material.name}/{part_name}: '
                            f'فعلی={pmr_stage} -> باید={cat_stage}'
                        )
        if fixable:
            print('قابل اصلاح:')
            for f in fixable:
                print(f'  {f}')
        else:
            print('هیچ اصلاحی لازم نیست.')

    print()
    print('=' * 80)
    print(f'STEP 1: برنامه‌ریزی آیتم شماره {ITEM_ID}')
    print('=' * 80)

    # --- Get expected requirements from BOM before planning ---
    print()
    print('--- پیش‌نیازهای ماده اولیه (از BOM) قبل از برنامه‌ریزی ---')
    expected = {}
    processes_used = set()
    for part_name, color_code in assignments:
        process = get_painting_process_for_color(color_code)
        if process:
            processes_used.add(process.id)
            reqs = PaintingMaterialRequirement.objects.filter(
                process_id=process.id,
                product_id=item.product_id,
                color_part=part_name,
            )
            for r in reqs:
                key = (r.raw_material.name, part_name)
                qty = r.consumption_per_unit * item.quantity
                expected[key] = expected.get(key, Decimal('0')) + qty
                stage_info = r.stage.name if r.stage else '(تمام مراحل)'
                print(f'  {r.raw_material.name} / {part_name} = {qty} '
                      f'(per_unit={r.consumption_per_unit}, stage={stage_info})')

    has_stage_null = any(
        r.stage_id is None
        for part_name, color_code in assignments
        for r in (get_painting_process_for_color(color_code) and
                  PaintingMaterialRequirement.objects.filter(
                      process_id=get_painting_process_for_color(color_code).id,
                      product_id=item.product_id,
                      color_part=part_name,
                  )) or []
    )

    if has_stage_null:
        print()
        print('  [یادداشت] برخی PMRها stage=None دارند؛ این مواد برای تمام مراحل اعتبار دارند.')

    # --- Check stages for this process ---
    print()
    print('--- مراحل روند نقاشی ---')
    for pid in processes_used:
        stages = PaintingStage.objects.filter(process_id=pid).order_by('order')
        proc = PaintingProcess.objects.get(id=pid)
        print(f'  روند: {proc.name}')
        for s in stages:
            print(f'    مرحله {s.order}: {s.name} (id={s.id})')

    # --- Check existing tasks before planning ---
    existing_before = ProductionTask.objects.filter(
        order_item=item, station_name='paint'
    )
    print()
    print(f'--- تعداد تسک‌های نقاشی قبل از برنامه‌ریزی: {existing_before.count()}')

    # --- Plan the item ---
    target_date = jdatetime.date.today()
    print()
    print(f'--- در حال برنامه‌ریزی برای تاریخ: {target_date} ---')
    if dry_run:
        print('--- حالت dry-run: برنامه‌ریزی انجام نمی‌شود ---')
        result = {'scheduled_count': existing_before.count(),
                  'scheduled_date': str(target_date),
                  'created_items': [], 'skipped': []}
    else:
        try:
            result = create_and_schedule_items_for_date([ITEM_ID], target_date)
            print(f'نتیجه برنامه‌ریزی: '
                  f'{json.dumps(result, default=str, ensure_ascii=False, indent=2)}')
        except Exception as e:
            print(f'خطا در برنامه‌ریزی: {e}')
            traceback.print_exc()
            return

    # === STEP 2: Check tasks created ===
    print()
    print('=' * 80)
    print('STEP 2: بررسی تسک‌های نقاشی ساخته شده')
    print('=' * 80)

    tasks = ProductionTask.objects.filter(
        order_item=item, station_name='paint'
    ).order_by('painting_stage__order')
    print(f'تعداد تسک‌های نقاشی: {tasks.count()}')
    for t in tasks:
        ps = t.painting_stage.name if t.painting_stage else 'بدون مرحله'
        print(f'  تسک id={t.id} | مرحله={ps} (id={t.painting_stage_id}) | '
              f'بخش رنگی={t.color_part} | تعداد={t.quantity} | وضعیت={t.status} | '
              f'تاریخ برنامه={t.scheduled_start} | کارگر={t.assigned_worker}')

    if not tasks:
        print('هیچ تسک نقاشی یافت نشد؛ خروج.')
        return

    # === STEP 2.5: Clean up orphaned queue rows (no sources) ===
    # After --fix-stages cleanup deletes old tasks, DailyMaterialQueueSource
    # rows are cascade-deleted. Queue rows that still have delivered_quantity > 0
    # survive the cleanup but become orphaned (no sources). These orphaned rows
    # with has_transaction=True block build_daily_queue_for_date from recreating
    # sources, causing the re-query to miss queue rows linked to new tasks.
    stage_ids_clean = [t.painting_stage_id for t in tasks if t.painting_stage_id]
    work_dates_set = set()
    for t in tasks:
        d = _inv_work_date(t.scheduled_start)
        if d:
            work_dates_set.add(d)
    if not dry_run and stage_ids_clean and work_dates_set:
        orphaned = DailyMaterialQueue.objects.filter(
            work_date__in=work_dates_set,
            painting_stage__in=stage_ids_clean,
            sources__isnull=True,
        ).distinct()
        orphaned_count = orphaned.count()
        if orphaned_count:
            print(f'\n  حذف {orphaned_count} ردیف صف سرافراز (بدون منبع) '
                  f'برای تاریخ‌های {sorted(work_dates_set)}...')
            orphaned.delete()

    # === STEP 3: Check warehouse queue ===
    print()
    print('=' * 80)
    print('STEP 3: بررسی مواد اولیه در صف انبار (DailyMaterialQueue)')
    print('=' * 80)

    sync_daily_material_queue_for_dates([target_date.togregorian()])

    stage_ids = [t.painting_stage_id for t in tasks if t.painting_stage_id]
    queue_rows = DailyMaterialQueue.objects.filter(
        painting_stage__in=stage_ids
    ) if stage_ids else DailyMaterialQueue.objects.none()
    queue_rows = queue_rows.filter(
        sources__production_task__order_item=item
    )

    print(f'ردیف‌های صف مواد نقاشی: {queue_rows.count()}')
    for q in queue_rows:
        color_part = q.color_part or '-'
        print(f'  {q.work_date} | کارگر={q.worker} | ماده={q.raw_material.name} | '
              f'مقدار={q.planned_quantity} | مرحله={q.painting_stage} | '
              f'بخش رنگی={color_part} | وضعیت={q.status}')

    # === STEP 4: Compare BOM vs queue per-stage ===
    print()
    print('=' * 80)
    print('STEP 4: مقایسه مقادیر مواد اولیه (BOM vs صف انبار) - بررسی به ازای هر مرحله')
    print('=' * 80)

    # For each stage + color_part, verify the queue quantity matches the BOM per-unit
    # (When PMR stage_id is None it applies to all stages; per-stage quantity should
    #  match consumption_per_unit * item.quantity)
    per_stage_checks = []
    per_stage_bom = {}

    for t in tasks:
        if not t.painting_stage:
            continue
        stage = t.painting_stage
        for part_name, color_code in assignments:
            if part_name != t.color_part:
                continue
            process = get_painting_process_for_color(color_code)
            if not process:
                continue
            reqs = PaintingMaterialRequirement.objects.filter(
                process_id=process.id,
                product_id=item.product_id,
                color_part=part_name,
            )
            for r in reqs:
                bom_qty = r.consumption_per_unit * item.quantity
                per_stage_bom.setdefault((stage.id, part_name), []).append(
                    (r.raw_material.name, bom_qty)
                )

    for r in queue_rows:
        stage_id = r.painting_stage_id
        color = r.color_part or ''
        key = (stage_id, color)
        # Find matching BOM entry
        bom_val = None
        raw_name = r.raw_material.name
        for mat_name, bom_qty in per_stage_bom.get(key, []):
            if mat_name == raw_name:
                bom_val = bom_qty
                break

        queue_qty = Decimal(str(r.planned_quantity))
        if bom_val is not None:
            match_char = 'OK' if bom_val == queue_qty else 'MISMATCH'
            per_stage_checks.append((match_char, False))
            print(f'  [{match_char}] مرحله={r.painting_stage.name} | '
                  f'ماده={raw_name}/{color} | BOM={bom_val} | صف={queue_qty}')
        else:
            # Material in queue that is not in BOM
            match_char = 'NOT-IN-BOM'
            per_stage_checks.append((match_char, True))
            print(f'  [{match_char}] مرحله={r.painting_stage.name} | '
                  f'ماده={raw_name}/{color} | BOM=N/A | صف={queue_qty}')

    mismatches = [m for m, _ in per_stage_checks if m != 'OK']
    print()
    if not mismatches:
        print('نتیجه: تمام مقادیر مواد اولیه در هر مرحله با BOM یکسان هستند.')
    else:
        print(f'نتیجه: {len(mismatches)} عدد عدم تطبیق یافت شد.')

    # === Aggregate comparison (shows correct totals per stage) ===
    print()
    print('--- خلاصه کل (BOM کل vs مجموع صف) ---')
    queue_totals = {}
    for q in queue_rows:
        key = (q.raw_material.name, q.color_part or '')
        queue_totals[key] = queue_totals.get(key, Decimal('0')) + Decimal(str(q.planned_quantity))

    header = f'{ "ماده اولیه":<20} { "بخش":<10} { "BOM":<10} { "صف انبار":<10} { "وضعیت":<10}'
    print(header)
    print('-' * 65)

    all_keys = sorted(set(list(expected.keys()) + list(queue_totals.keys())))
    for key in all_keys:
        mat_name, color = key
        bom_qty = expected.get(key, Decimal('0'))
        queue_qty = queue_totals.get(key, Decimal('0'))
        if bom_qty == queue_qty:
            match_char = 'OK'
        elif queue_qty > 0 and bom_qty == 0:
            match_char = 'COLOR-VARIANT'
        else:
            match_char = 'DIFF'
        print(f'{mat_name:<20} {color:<10} {str(bom_qty):<10} {str(queue_qty):<10} {match_char}')

    stage_null_count = sum(
        1 for part_name, color_code in assignments
        for r in PaintingMaterialRequirement.objects.filter(
            process_id=get_painting_process_for_color(color_code).id if get_painting_process_for_color(color_code) else 0,
            product_id=item.product_id, color_part=part_name,
        ) if r.stage_id is None
    )
    if stage_null_count > 0:
        print()
        print(f'  {stage_null_count} ماده دارای stage=NULL هستند (برای تمام مراحل)؛ '
              f'بقیه فقط در مرحله مربوطه می‌آیند.')

    # === STEP 5: Check alignment ===
    print()
    print('=' * 80)
    print('STEP 5: بررسی هماهنگی برنامه‌ریزی نقاشی با صف مواد انبار')
    print('=' * 80)

    task_stages_map = {}
    for t in tasks:
        if t.painting_stage:
            task_stages_map.setdefault(t.painting_stage_id, []).append(t)

    queue_by_stage = {}
    for q in queue_rows:
        queue_by_stage.setdefault(q.painting_stage_id, []).append(q)

    print('بررسی مرحله به مرحله:')
    for stage_id, stage_tasks in sorted(task_stages_map.items()):
        stage = PaintingStage.objects.get(id=stage_id)
        stage_queue = queue_by_stage.get(stage_id, [])
        print(f'  مرحله {stage.order}: {stage.name}')
        print(f'    تعداد تسک: {len(stage_tasks)}')
        print(f'    ردیف‌های صف: {len(stage_queue)}')
        for ft in stage_tasks:
            has_coverage = len(stage_queue) > 0
            status_str = 'پوشش دارد' if has_coverage else 'بدون پوشش'
            print(f'    تسک {ft.id} ({ft.color_part}): {status_str}')

    if not queue_rows:
        print('  هشدار: هیچ ردیف صف انباری برای مراحل نقاشی یافت نشد!')
    else:
        print(f'کل ردیف‌های صف: {queue_rows.count()} - '
              f'برنامه‌ریزی و صف انبار همگام هستند.')

    # === Final report ===
    print()
    print('=' * 80)
    print('گزارش نهایی کامل')
    print('=' * 80)
    print(f'آیتم: {item}')
    print(f'محصول: {item.product} (id={item.product_id})')
    print(f'تعداد سفارش: {item.quantity}')
    print(f'تعداد تسک‌های نقاشی: {tasks.count()}')

    # Count distinct stages from tasks
    num_stages = tasks.filter(painting_stage__isnull=False).values_list(
        'painting_stage', flat=True
    ).distinct().count()
    print(f'تعداد مراحل نقاشی: {num_stages}')
    print(f'تعداد ردیف‌های صف انبار: {queue_rows.count()}')

    # Per-stage check: OK means quantity matches; NOT-IN-BOM means color variant name mismatch
    real_mismatches = [m for m, is_name_diff in per_stage_checks
                       if m == 'MISMATCH']
    name_mismatches = [m for m, is_name_diff in per_stage_checks
                       if m == 'NOT-IN-BOM']
    print(f'مطابقت مقدار مواد در هر مرحله: '
          f'{"بله" if not real_mismatches else "نه"} '
          f'(تعداد عدم تطبیق مقدار: {len(real_mismatches)})')
    if name_mismatches:
        print(f'  ({len(name_mismatches)} مورد نام ماده به‌صورت color-variant حل شده است)')
    print(f'برنامه‌ریزی با صف انبار همگام است: {queue_rows.count() > 0}')

    # === STEP 6: Daily Material Delivery Simulation ===
    print()
    print('=' * 80)
    print('STEP 6: شبیه‌سازی تحویل مواد اولیه به کارگران (روزانه)')
    print('=' * 80)

    warehouse_user = User.objects.filter(is_staff=True).first()
    if not warehouse_user:
        print('هشدار: هیچ کاربر انباری یافت نشد. از کاربر پیش‌فرض استفاده می‌شود.')
        warehouse_user = User.objects.first()

    # Sync queue for ALL task dates so we have rows to simulate against
    from django.utils import timezone
    task_dates = set()
    for t in tasks:
        if t.scheduled_start:
            d = _inv_work_date(t.scheduled_start)
            if d:
                task_dates.add(d)
    if task_dates:
        print(f'همگام‌سازی صف برای {len(task_dates)} تاریخ کاری: '
              f'{sorted(task_dates)}')
        sync_daily_material_queue_for_dates(list(task_dates))

    # Re-query queue rows after sync across all dates
    queue_rows = DailyMaterialQueue.objects.filter(
        sources__production_task__order_item=item
    ).distinct()

    # Ensure sufficient stock for delivery — calculate total need per material
    print()
    print('--- اطمینان از موجودی کافی برای تحویل ---')

    # Aggregate total physical need per raw_material across all queue rows
    # Uses _physical_for with stock context: when stock has a partial pack
    # (stock % pack_size > 0), physical = need (exact); otherwise rounds up.
    material_needs = {}  # raw_material_id -> total_physical
    for q in queue_rows:
        mat = q.raw_material
        pack = Decimal(str(mat.pack_size or 0))
        planned = Decimal(str(q.planned_quantity))
        stock = Decimal(str(mat.current_stock))
        _packs, physical = _physical_for(planned, float(pack) if pack > 0 else 0, stock)
        material_needs[mat.id] = material_needs.get(mat.id, Decimal('0')) + Decimal(str(physical))

    for mat_id, total_physical in material_needs.items():
        mat = RawMaterial.objects.get(id=mat_id)
        current = Decimal(str(mat.current_stock))
        short = total_physical - current
        if short > 0:
            added_qty = short + Decimal('10')
            StockMovement.objects.create(
                raw_material=mat,
                movement_type='purchase',
                quantity=added_qty,
                note='مرجع شبیه‌سازی: افزودن موجودی برای تحویل روزانه',
            )
            new_stock = Decimal(str(mat.current_stock))
            print(f'  افزوده شد: {mat.name} → +{added_qty} '
                  f'(موجودی قبلی={current} → جدید={new_stock}، نیاز کل={total_physical})')
        else:
            print(f'  کافی: {mat.name} → موجودی={current}، نیاز کل={total_physical}')

    # Simulate per-day delivery and return
    # Group queue rows by work date
    queue_by_date = {}
    for q in queue_rows:
        d = q.work_date
        queue_by_date.setdefault(d, []).append(q)

    delivery_log = []
    return_log = []
    underdelivered_rows = []

    sorted_dates = sorted(queue_by_date.keys())
    first_date = sorted_dates[0] if sorted_dates else None

    for work_date in sorted_dates:
        day_queues = queue_by_date[work_date]
        print()
        print(f'--- تاریخ کاری: {work_date} ---')
        print(f'  ردیف‌های صف این روز: {len(day_queues)}')

        for q in day_queues:
            mat = q.raw_material
            pack = _q2(mat.pack_size or 0)
            planned = _q2(q.planned_quantity)
            stock = _q2(mat.current_stock)

            packs, physical = _physical_for(planned, pack, stock)

            print(f'  مقدار: {mat.name} / {q.worker} | planned={planned} | '
                  f'pack={pack} | packs={packs} | physical(تحویل)={physical} | '
                  f'material={mat.name}')

            # Under-delivery test: skip one material on the first work date
            if (not underdelivered_rows
                    and mat.name == 'تینر معمولی'
                    and first_date and work_date == first_date):
                print(f'    ⚠️ عمدی: تحویل این ماده اسکیپ شد '
                      f'(تحویل ناقص برای تست حالت under-delivery)')
                underdelivered_rows.append(q)
                continue

            try:
                execute_daily_delivery(
                    queue_id=q.pk,
                    delivered_by=warehouse_user,
                    note=f'تحویل روزانه {work_date}',
                )
                q.refresh_from_db()
                delivery_log.append({
                    'queue_id': q.pk,
                    'date': str(work_date), 'material': mat.name,
                    'worker': q.worker, 'planned': planned,
                    'delivered': Decimal(str(q.delivered_quantity)),
                    'excess': Decimal(str(q.excess_consumption)),
                    'status': q.status,
                })
                print(f'    ✅ تحویل شد: delivered={q.delivered_quantity} '
                      f'excess_consumption={q.excess_consumption} '
                      f'status={q.status}')
            except HandoverError as e:
                print(f'    ❌ خطا: {e}')

    # === STEP 7: End-of-Day Return Simulation ===
    print()
    print('=' * 80)
    print('STEP 7: شبیه‌سازی بازگشت مواد در پایان روز')
    print('=' * 80)

    for entry in delivery_log:
        try:
            q = DailyMaterialQueue.objects.get(pk=entry['queue_id'])
        except DailyMaterialQueue.DoesNotExist:
            continue

        # Return 50% of delivered as "unused"
        delivered = Decimal(str(q.delivered_quantity))
        return_qty = delivered * Decimal('0.5')
        if return_qty <= 0:
            continue

        try:
            execute_daily_return(
                queue_id=q.pk,
                returned_by=warehouse_user,
                returned_quantity=return_qty,
                note='بازگشت نیمه‌ای در پایان روز',
            )
            q.refresh_from_db()
            actual = q.actual_consumption
            excess = q.excess_consumption
            returned = Decimal(str(q.returned_quantity))
            print(f'  {entry["date"]} | {entry["material"]} / {q.worker}: '
                  f'delivered={q.delivered_quantity} returned={returned} '
                  f'actual={actual} excess={excess} status={q.status}')
            return_log.append({
                'date': entry['date'], 'material': entry['material'],
                'worker': q.worker, 'delivered': q.delivered_quantity,
                'returned': q.returned_quantity,
                'actual': actual, 'excess': excess,
            })
        except HandoverError as e:
            print(f'  {entry["date"]} | {entry["material"]} / {q.worker}: '
                  f'❌ {e}')

    # === STEP 8: Under-Delivery Scenario ===
    print()
    print('=' * 80)
    print('STEP 8: سناریوی تحویل ناقص (under-delivery)')
    print('=' * 80)

    if underdelivered_rows:
        skipped = underdelivered_rows[0]
        skipped.refresh_from_db()
        print(f'ردیف مورد نظر (اسکیپ شده):')
        print(f'  {skipped.work_date} | {skipped.raw_material.name} / '
              f'{skipped.worker} | planned={skipped.planned_quantity} '
              f'delivered={skipped.delivered_quantity} | status={skipped.status}')

        # Sync to next day and check persistence
        next_date = skipped.work_date + timedelta(days=1)
        print(f'\nهمگام‌سازی صف برای تاریخ بعدی ({next_date})...')
        sync_daily_material_queue_for_dates([next_date])

        # Check if the skipped row still exists
        try:
            skipped.refresh_from_db()
            print(f'ردیف همچنان وجود دارد: status={skipped.status} '
                  f'(planned={skipped.planned_quantity}, delivered={skipped.delivered_quantity})')
            if skipped.status == 'pending':
                print('  → هشدار: این ردیف هنوز تحویل داده نشده است.')
                print('  → راه‌حل: تحویل دادن در روز بعد یا ادامه کار')
        except Exception:
            print('ردیف حذف شده است.')

        # Now deliver the skipped row
        try:
            execute_daily_delivery(
                queue_id=skipped.pk,
                delivered_by=warehouse_user,
                note='تحویل معوقه از روز قبل',
            )
            skipped.refresh_from_db()
            print(f'  ✅ تحویل معوقه شد: delivered={skipped.delivered_quantity} '
                  f'status={skipped.status}')
        except HandoverError as e:
            print(f'  ❌ خطا در تحویل معوقه: {e}')
    else:
        print('هیچ ردیفی در وضعیت under-delivery یافت نشد.')

    # === STEP 9: Over-Delivery / Excess Consumption ===
    print()
    print('=' * 80)
    print('STEP 9: سناریوی مازاد مصرف (over-delivery / excess_consumption)')
    print('=' * 80)

    print('ردیف‌هایی که مازاد مصرف داشته‌اند (delivered > planned):')
    found_excess = False
    for entry in delivery_log:
        if entry['excess'] and Decimal(str(entry['excess'])) > 0:
            found_excess = True
            print(f'  {entry["date"]} | {entry["material"]} / {entry["worker"]} | '
                  f'planned={entry["planned"]} | delivered={entry["delivered"]} | '
                  f'excess_consumption={entry["excess"]}')
    if not found_excess:
        print('  هیچ مورد مازاد مصرف یافت نشد.')

    print()
    print('🔍 توضیح عملکرد:')
    print('  - excess_consumption = max(0, delivered - returned - planned)')
    print('  - این مقدار در فیلد excess_consumption ردیف صف ذخیره می‌شود')
    print('  - ماده‌های پتری (pack_size > 0) معمولاً مازاد دارند زیرا گرد می‌شوند به بسته کامل')
    print('  - مازاد مصرف به انبار عمومی برمی‌گردد اگر برگشت داده شود')


if __name__ == '__main__':
    main()
