# -*- coding: utf-8 -*-
"""Fix create_and_schedule sync hook in product/utils.py."""

with open("product/utils.py", encoding="utf-8") as f:
    src = f.read()

# Find the return block after schedule_paint_items_auto
old = "cnt, last_date, _ = schedule_paint_items_auto("
idx = src.find(old)
if idx == -1:
    print("ERROR: schedule_paint_items_auto not found")
else:
    # Find the return statement after this
    insert_before = "        return {"
    insert_pos = src.find(insert_before, idx)
    if insert_pos != -1:
        sync_code = (
            "\n        # Phase 3: sync DailyMaterialQueue for all affected dates\n"
            "        scheduled_task_ids = list(\n"
            "            ProductionTask.objects.filter(\n"
            "                order_item_id__in=item_ids,\n"
            "                station_name='paint',\n"
            "                scheduled_start__isnull=False,\n"
            "            ).values_list('id', flat=True)\n"
            "        )\n"
            "        _sync_queue_for_task_ids(scheduled_task_ids)\n"
        )
        src = src[:insert_pos] + sync_code + src[insert_pos:]
        with open("product/utils.py", "w", encoding="utf-8") as f:
            f.write(src)
        print("Added sync to create_and_schedule_items_for_date")
    else:
        print("ERROR: return anchor not found")

import ast
with open("product/utils.py", encoding="utf-8") as f:
    src2 = f.read()
try:
    ast.parse(src2)
    print("utils.py parses OK, lines:", src2.count(chr(10)))
except SyntaxError as e:
    print("SYNTAX ERROR:", e)