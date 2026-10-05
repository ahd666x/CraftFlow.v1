import sqlite3
import json

conn = sqlite3.connect('db.sqlite3')
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("=" * 80)
print("SECTION 7: DATABASE INTEGRITY SNAPSHOT")
print("=" * 80)

# --- DailyMaterialQueue counts ---
print("\n--- DailyMaterialQueue ---")
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueue")
print(f"Total DailyMaterialQueue rows: {c.fetchone()['cnt']}")

c.execute("SELECT status, COUNT(*) as cnt FROM inventory_dailymaterialqueue GROUP BY status")
for r in c.fetchall():
    print(f"  Status '{r['status']}': {r['cnt']}")

# delivered rows
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueue WHERE delivered_quantity > 0")
print(f"Delivered rows (delivered_quantity > 0): {c.fetchone()['cnt']}")

# returned rows
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueue WHERE returned_quantity > 0")
print(f"Returned rows (returned_quantity > 0): {c.fetchone()['cnt']}")

# actual consumption rows (non-zero)
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueue WHERE actual_consumption > 0")
print(f"Actual consumption rows (actual_consumption > 0): {c.fetchone()['cnt']}")

# queue rows with missing task (no sources)
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueue q WHERE NOT EXISTS (SELECT 1 FROM inventory_dailymaterialqueuesource s WHERE s.queue_id = q.id)")
print(f"Queue rows with no sources (missing task): {c.fetchone()['cnt']}")

# queue rows with missing raw material — raw_material_id is FK NOT NULL, so check raw_material_id IS NULL
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueue WHERE raw_material_id IS NULL")
print(f"Queue rows with missing raw_material_id: {c.fetchone()['cnt']}")

# queue rows with missing stage — DailyMaterialQueue doesn't have a stage field directly,
# but sources have painting_stage. Check sources with missing stage
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueuesource WHERE painting_stage_id IS NULL")
print(f"Queue sources with missing painting_stage_id: {c.fetchone()['cnt']}")

# orphan sources (source not linked to a queue)
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueuesource s WHERE s.queue_id NOT IN (SELECT id FROM inventory_dailymaterialqueue)")
print(f"Orphan queue sources (queue_id does not exist): {c.fetchone()['cnt']}")

# duplicate queue keys (work_date, worker, raw_material should be unique)
c.execute("""
    SELECT work_date, worker_id, raw_material_id, COUNT(*) as cnt
    FROM inventory_dailymaterialqueue
    GROUP BY work_date, worker_id, raw_material_id
    HAVING COUNT(*) > 1
""")
dups = c.fetchall()
print(f"Duplicate queue keys: {len(dups)}")
for r in dups:
    print(f"  work_date={r['work_date']}, worker_id={r['worker_id']}, raw_material_id={r['raw_material_id']}: {r['cnt']} rows")

# source/planned-quantity mismatches
c.execute("""
    SELECT q.id, q.work_date, q.worker_id, q.raw_material_id, 
           q.planned_quantity, s_sum.total_sources
    FROM inventory_dailymaterialqueue q
    LEFT JOIN (
        SELECT queue_id, SUM(quantity) as total_sources
        FROM inventory_dailymaterialqueuesource
        GROUP BY queue_id
    ) s_sum ON s_sum.queue_id = q.id
    WHERE s_sum.total_sources IS NOT NULL
      AND ABS(CAST(q.planned_quantity AS REAL) - CAST(s_sum.total_sources AS REAL)) > 0.001
""")
mismatches = c.fetchall()
print(f"Source/planned-quantity mismatches: {len(mismatches)}")
for r in mismatches:
    print(f"  queue#{r['id']} (date={r['work_date']}, worker={r['worker_id']}, raw={r['raw_material_id']}): planned={r['planned_quantity']} vs sources_sum={r['total_sources']}")

# --- DailyMaterialQueueSource ---
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueuesource")
print(f"\nTotal DailyMaterialQueueSource rows: {c.fetchone()['cnt']}")

# sources with missing task
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueuesource WHERE production_task_id IS NULL")
print(f"Sources with missing production_task: {c.fetchone()['cnt']}")

# sources with missing raw material
c.execute("SELECT COUNT(*) as cnt FROM inventory_dailymaterialqueuesource WHERE raw_material_id IS NULL")
print(f"Sources with missing raw_material: {c.fetchone()['cnt']}")

# --- MaterialIssue ---
print("\n--- MaterialIssue ---")
c.execute("SELECT COUNT(*) as cnt FROM inventory_materialissue")
print(f"Total MaterialIssue: {c.fetchone()['cnt']}")

c.execute("SELECT purpose, COUNT(*) as cnt FROM inventory_materialissue GROUP BY purpose")
for r in c.fetchall():
    print(f"  purpose='{r['purpose']}': {r['cnt']}")

c.execute("SELECT status, COUNT(*) as cnt FROM inventory_materialissue GROUP BY status")
for r in c.fetchall():
    print(f"  status='{r['status']}': {r['cnt']}")

# production-purpose issues
c.execute("SELECT COUNT(*) as cnt FROM inventory_materialissue WHERE purpose='production'")
print(f"Production purpose: {c.fetchone()['cnt']}")

# rework-purpose issues
c.execute("SELECT COUNT(*) as cnt FROM inventory_materialissue WHERE purpose='rework'")
print(f"Rework purpose: {c.fetchone()['cnt']}")

# painting-linked issues (task is paint or order_item-based with station paint)
c.execute("""
    SELECT COUNT(*) as cnt FROM inventory_materialissue 
    WHERE purpose='production' AND (task_id IS NULL AND order_item_id IS NOT NULL)
""")
print(f"Painting-linked (order_item-based, task=None, purpose=production): {c.fetchone()['cnt']}")

# normal-painting-linked (purpose=production, task is a paint task)
c.execute("""
    SELECT COUNT(*) as cnt FROM inventory_materialissue 
    WHERE purpose='production' AND task_id IS NOT NULL 
    AND task_id IN (SELECT id FROM product_productiontask WHERE station_name='paint')
""")
print(f"Normal-painting-linked (purpose=production, task is paint): {c.fetchone()['cnt']}")

# issues with StockMovement
c.execute("""
    SELECT COUNT(*) as cnt FROM inventory_materialissue 
    WHERE id IN (SELECT DISTINCT fulfilled_issue_id FROM inventory_stockmovement WHERE fulfilled_issue_id IS NOT NULL)
""")
print(f"Issues with StockMovement (fulfilled_issue link): {c.fetchone()['cnt']}")

# issues without StockMovement (issued/partial status but no movement)
c.execute("""
    SELECT COUNT(*) as cnt FROM inventory_materialissue 
    WHERE status IN ('issued', 'partial') 
    AND id NOT IN (SELECT DISTINCT fulfilled_issue_id FROM inventory_stockmovement WHERE fulfilled_issue_id IS NOT NULL)
""")
print(f"Issues without StockMovement (issued/partial but no movement): {c.fetchone()['cnt']}")

# --- StockMovement ---
print("\n--- StockMovement ---")
c.execute("SELECT COUNT(*) as cnt FROM inventory_stockmovement")
print(f"Total StockMovement: {c.fetchone()['cnt']}")

c.execute("SELECT movement_type, COUNT(*) as cnt FROM inventory_stockmovement GROUP BY movement_type")
for r in c.fetchall():
    print(f"  movement_type='{r['movement_type']}': {r['cnt']}")

# purchases
c.execute("SELECT COUNT(*) as cnt FROM inventory_stockmovement WHERE movement_type='purchase'")
print(f"Purchases: {c.fetchone()['cnt']}")

# consumption
c.execute("SELECT COUNT(*) as cnt FROM inventory_stockmovement WHERE movement_type='consumption'")
print(f"Consumption: {c.fetchone()['cnt']}")

# returns
c.execute("SELECT COUNT(*) as cnt FROM inventory_stockmovement WHERE movement_type='return'")
print(f"Returns: {c.fetchone()['cnt']}")

# adjustments
c.execute("SELECT COUNT(*) as cnt FROM inventory_stockmovement WHERE movement_type='adjustment'")
print(f"Adjustments: {c.fetchone()['cnt']}")

# painting-linked consumption (reference_task is a paint task, OR reference_order_item is set)
c.execute("""
    SELECT COUNT(*) as cnt FROM inventory_stockmovement 
    WHERE movement_type='consumption' 
    AND (
        reference_task_id IN (SELECT id FROM product_productiontask WHERE station_name='paint')
        OR reference_order_item_id IS NOT NULL
    )
""")
print(f"Painting-linked consumption: {c.fetchone()['cnt']}")

# consumption with task reference
c.execute("SELECT COUNT(*) as cnt FROM inventory_stockmovement WHERE movement_type='consumption' AND reference_task_id IS NOT NULL")
print(f"Consumption with task reference: {c.fetchone()['cnt']}")

# consumption with order_item reference
c.execute("SELECT COUNT(*) as cnt FROM inventory_stockmovement WHERE movement_type='consumption' AND reference_order_item_id IS NOT NULL")
print(f"Consumption with order_item reference: {c.fetchone()['cnt']}")

# consumption with issue reference
c.execute("SELECT COUNT(*) as cnt FROM inventory_stockmovement WHERE movement_type='consumption' AND fulfilled_issue_id IS NOT NULL")
print(f"Consumption with issue reference (fulfilled_issue): {c.fetchone()['cnt']}")

# consumption with NO operational reference
c.execute("""
    SELECT COUNT(*) as cnt FROM inventory_stockmovement 
    WHERE movement_type='consumption' 
    AND reference_task_id IS NULL 
    AND reference_order_item_id IS NULL 
    AND fulfilled_issue_id IS NULL
""")
print(f"Consumption with no operational reference: {c.fetchone()['cnt']}")

conn.close()
