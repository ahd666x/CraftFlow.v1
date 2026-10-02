# -*- coding: utf-8 -*-
"""Add Phase 3 sync hooks to product/utils.py and product/views.py."""

# === 1. Add sync import to product/utils.py ===
with open("product/utils.py", encoding="utf-8") as f:
    utils_src = f.read()

# Add sync import after the existing imports
sync_import = (
    "\n\n# Phase 3: Daily Material Queue sync hook\n"
    "def _sync_queue_for_task_ids(task_ids):\n"
    '    """Best-effort sync of DailyMaterialQueue for affected dates."""\n'
    "    try:\n"
    "        from inventory.services import sync_queue_for_tasks\n"
    "        return sync_queue_for_tasks(task_ids)\n"
    "    except Exception:\n"
    "        logger.exception('_sync_queue_for_task_ids: sync failed')\n"
    "        return []\n"
    "\n"
    "\n"
    "def _sync_queue_for_date(date):\n"
    '    """Best-effort sync of DailyMaterialQueue for a single date."""\n'
    "    try:\n"
    "        from inventory.services import sync_queue_for_date\n"
    "        return sync_queue_for_date(date)\n"
    "    except Exception:\n"
    "        logger.exception('_sync_queue_for_date: sync failed')\n"
    "        return []\n"
)

# Insert before the first function definition (after imports)
# Find a good insertion point: after "logger = logging.getLogger(__name__)"
anchor = "logger = logging.getLogger(__name__)"
if anchor in utils_src:
    idx = utils_src.find(anchor)
    # Find end of line
    end = utils_src.find("\n", idx) + 1
    utils_src = utils_src[:end] + sync_import + utils_src[end:]
    print("Added sync helpers to utils.py")
else:
    print("ERROR: anchor not found in utils.py")

# === 2. Add sync call to create_and_schedule_items_for_date ===
# After schedule_paint_items_auto returns, sync affected dates
old = """        cnt, last_date, _ = schedule_paint_items_auto(
            all_item_ids, target_date, max_days=100, initial_item_cursors=initial_item_cursors
        )

        return {
            'scheduled_count': cnt,
            'scheduled_date': last_date or target_date,
            'created_items': created_items,
            'skipped': skipped
        }"""

new = """        cnt, last_date, _ = schedule_paint_items_auto(
            all_item_ids, target_date, max_days=100, initial_item_cursors=initial_item_cursors
        )

        # Phase 3: sync DailyMaterialQueue for all affected dates
        scheduled_task_ids = list(
            ProductionTask.objects.filter(
                order_item_id__in=item_ids,
                station_name='paint',
                scheduled_start__isnull=False,
            ).values_list('id', flat=True)
        )
        _sync_queue_for_task_ids(scheduled_task_ids)

        return {
            'scheduled_count': cnt,
            'scheduled_date': last_date or target_date,
            'created_items': created_items,
            'skipped': skipped
        }"""

if old in utils_src:
    utils_src = utils_src.replace(old, new, 1)
    print("Added sync to create_and_schedule_items_for_date")
else:
    print("ERROR: create_and_schedule anchor not found")

# === 3. Add sync call to assign_task_to_worker ===
# After the cascade scheduling completes, sync affected dates
old2 = """        final_start, final_end = changes[task.pk][1], changes[task.pk][2]
        return {
            'ok': True,
            'scheduled_start': timezone.localtime(final_start).strftime('%H:%M'),
            'scheduled_end': timezone.localtime(final_end).strftime('%H:%M'),
            'shifted_tasks_count': len(changes) - 1 + source_tasks_count + dest_tasks_count,
        }"""

new2 = """        final_start, final_end = changes[task.pk][1], changes[task.pk][2]

        # Phase 3: sync DailyMaterialQueue for affected dates
        affected_ids = list(changes.keys())
        _sync_queue_for_task_ids(affected_ids)

        return {
            'ok': True,
            'scheduled_start': timezone.localtime(final_start).strftime('%H:%M'),
            'scheduled_end': timezone.localtime(final_end).strftime('%H:%M'),
            'shifted_tasks_count': len(changes) - 1 + source_tasks_count + dest_tasks_count,
        }"""

if old2 in utils_src:
    utils_src = utils_src.replace(old2, new2, 1)
    print("Added sync to assign_task_to_worker")
else:
    print("ERROR: assign_task_to_worker anchor not found")

# === 4. Add sync call to reschedule_worker_tasks_on_date ===
# After bulk_update, sync affected date
old3 = """                if to_update:
                    ProductionTask.objects.bulk_update(
                        to_update,
                        ['assigned_worker_id', 'scheduled_start', 'scheduled_end']
                    )

                return len(to_update)"""

new3 = """                if to_update:
                    ProductionTask.objects.bulk_update(
                        to_update,
                        ['assigned_worker_id', 'scheduled_start', 'scheduled_end']
                    )

                    # Phase 3: sync DailyMaterialQueue for affected date
                    _sync_queue_for_date(gregorian)

                return len(to_update)"""

if old3 in utils_src:
    utils_src = utils_src.replace(old3, new3, 1)
    print("Added sync to reschedule_worker_tasks_on_date")
else:
    print("ERROR: reschedule_worker_tasks_on_date anchor not found")

with open("product/utils.py", "w", encoding="utf-8") as f:
    f.write(utils_src)

print("utils.py updated")

import ast
with open("product/utils.py", encoding="utf-8") as f:
    src2 = f.read()
try:
    ast.parse(src2)
    print("utils.py parses OK, lines:", src2.count(chr(10)))
except SyntaxError as e:
    print("SYNTAX ERROR in utils.py:", e)