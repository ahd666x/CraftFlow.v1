import sqlite3
import sys
import codecs

# Use UTF-8 for stdout
sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer)

conn = sqlite3.connect('db.sqlite3')
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("=== Unreferenced Consumption Movements ===")
c.execute("""
    SELECT id, raw_material_id, movement_type, quantity, note, created_at, 
           reference_task_id, reference_order_item_id, fulfilled_issue_id,
           created_by_id
    FROM inventory_stockmovement 
    WHERE movement_type='consumption' 
    AND reference_task_id IS NULL 
    AND reference_order_item_id IS NULL 
    AND fulfilled_issue_id IS NULL
    ORDER BY id
""")
rows = c.fetchall()
for r in rows:
    print(f"\n  ID: {r['id']}")
    print(f"  raw_material_id: {r['raw_material_id']}")
    print(f"  quantity: {r['quantity']}")
    print(f"  note: {r['note']}")
    print(f"  created_at: {r['created_at']}")
    print(f"  created_by_id: {r['created_by_id']}")
    print(f"  reference_task_id: {r['reference_task_id']}")
    print(f"  reference_order_item_id: {r['reference_order_item_id']}")
    print(f"  fulfilled_issue_id: {r['fulfilled_issue_id']}")

c.execute("SELECT id, name FROM inventory_rawmaterial ORDER BY id")
mat_names = {r['id']: r['name'] for r in c.fetchall()}
print("\nRaw materials map:", {k: v for k, v in sorted(mat_names.items())})

print("\n=== ALL Consumption Movements ===")
c.execute("""
    SELECT id, raw_material_id, quantity, note, created_at, 
           reference_task_id, reference_order_item_id, fulfilled_issue_id
    FROM inventory_stockmovement 
    WHERE movement_type='consumption' 
    ORDER BY id
""")
for r in c.fetchall():
    print(f"\n  ID: {r['id']}")
    print(f"  raw_material: {mat_names.get(r['raw_material_id'], '?')} (id={r['raw_material_id']})")
    print(f"  quantity: {r['quantity']}")
    print(f"  note: {r['note']}")
    print(f"  created_at: {r['created_at']}")
    print(f"  ref_task: {r['reference_task_id']}, ref_order_item: {r['reference_order_item_id']}, fulfilled_issue: {r['fulfilled_issue_id']}")

print("\n=== ALL StockMovements ===")
c.execute("""
    SELECT id, movement_type, raw_material_id, quantity, note, created_at,
           reference_task_id, reference_order_item_id, fulfilled_issue_id,
           reference_color_part, created_by_id
    FROM inventory_stockmovement 
    ORDER BY id
""")
for r in c.fetchall():
    print(f"\n  ID: {r['id']}")
    print(f"  type: {r['movement_type']}")
    print(f"  raw_material: {mat_names.get(r['raw_material_id'], '?')} (id={r['raw_material_id']})")
    print(f"  quantity: {r['quantity']}")
    print(f"  note: {r['note']}")
    print(f"  created_at: {r['created_at']}")
    print(f"  ref_task: {r['reference_task_id']}, ref_order_item: {r['reference_order_item_id']}, fulfilled_issue: {r['fulfilled_issue_id']}")
    print(f"  reference_color_part: {r['reference_color_part']}, created_by_id: {r['created_by_id']}")

print("\n=== MaterialIssue details ===")
c.execute("""
    SELECT id, purpose, status, task_id, order_item_id, defect_id, 
           color_part, raw_material_id, requested_quantity, issued_quantity,
           note, created_at
    FROM inventory_materialissue 
    ORDER BY id
""")
for r in c.fetchall():
    print(f"\n  ID: {r['id']}")
    print(f"  purpose: {r['purpose']}, status: {r['status']}")
    print(f"  task_id: {r['task_id']}, order_item_id: {r['order_item_id']}, defect_id: {r['defect_id']}")
    print(f"  color_part: {r['color_part']}, raw_material_id: {r['raw_material_id']}")
    print(f"  requested_qty: {r['requested_quantity']}, issued_qty: {r['issued_quantity']}")
    print(f"  note: {r['note']}")
    print(f"  created_at: {r['created_at']}")

print("\n=== Issues with StockMovement linkage ===")
c.execute("""
    SELECT m.id as movement_id, m.movement_type, m.quantity, m.note as mvt_note, m.created_at,
           m.reference_task_id, m.reference_order_item_id, m.fulfilled_issue_id,
           i.purpose, i.status, i.task_id, i.order_item_id, i.defect_id
    FROM inventory_stockmovement m
    JOIN inventory_materialissue i ON m.fulfilled_issue_id = i.id
    ORDER BY m.id
""")
for r in c.fetchall():
    print(f"\n  Movement ID: {r['movement_id']}")
    print(f"  Movement type: {r['movement_type']}, qty: {r['quantity']}")
    print(f"  Movement note: {r['mvt_note']}")
    print(f"  Movement created_at: {r['created_at']}")
    print(f"  ref_task: {r['reference_task_id']}, ref_order_item: {r['reference_order_item_id']}")
    print(f"  Issue: purpose={r['purpose']}, status={r['status']}, task={r['task_id']}, order_item={r['order_item_id']}, defect={r['defect_id']}")

# Check DailyMaterialQueue rows for detail
print("\n=== DailyMaterialQueue rows ===")
c.execute("""
    SELECT id, work_date, worker_id, raw_material_id, planned_quantity, 
           delivered_quantity, returned_quantity, actual_consumption, status
    FROM inventory_dailymaterialqueue 
    ORDER BY id
""")
for r in c.fetchall():
    print(f"  id={r['id']}, date={r['work_date']}, worker={r['worker_id']}, raw={mat_names.get(r['raw_material_id'], '?')}, "
          f"planned={r['planned_quantity']}, delivered={r['delivered_quantity']}, returned={r['returned_quantity']}, "
          f"actual={r['actual_consumption']}, status={r['status']}")

conn.close()
