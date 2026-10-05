# P9 — Read-Only Audit: Inventory-Consumption Bypass Paths

## Executive Summary

**Decision: P9-B** — One open-risk path identified (W12), but it has mitigating controls. No immediate data corruption found. Recommended fix documented below.

---

## 1. Audit Scope & Methodology

**Objective:** Identify every code path that creates a `StockMovement` with `movement_type='consumption'`, and determine whether each path can bypass the P4/P5/P7 normal-painting contract — i.e., consume material already delivered (or pending delivery) through the `DailyMaterialQueue` (Engine B), causing double-counted warehouse stock decrement.

**Method:** Exhaustive static analysis of all `StockMovement.objects.create(...)` and `MaterialIssue.objects.create(...)` call sites across the entire codebase, plus read-only inspection of the live SQLite database (`db.sqlite3`) to confirm current state.

**Flag in scope:** `NORMAL_PAINTING_ENGINE_IS_DAILY_QUEUE = True` (hardcoded at `product/utils.py:2445`). This means paint materials are normally delivered through Engine B (DailyMaterialQueue), NOT through Engine A (MaterialIssue handover).

**Key helper:** `is_paint_need_delivered_by_queue(task, raw_material)` at `inventory/services.py:1557` — returns `True` when a given paint task's material requirement for a given raw material is managed by Engine B. This is the P7 guard predicate.

---

## 2. StockMovement Writer Classification

| ID | Writer | File:Line | Movement Type | Links to Task/Issue/Order | Paint Guard | Status |
|----|--------|-----------|---------------|---------------------------|-------------|--------|
| W1 | `production_issue_queue` (request) | `inventory/views.py:199` | *(MaterialIssue)* | `task` FK | **P4** (blocks paint tasks at line 183) | ✅ Safe |
| W2 | `handover_create` | `inventory/views.py:377` | consumption | `fulfilled_issue`, `reference_task`/`reference_order_item` | P4 (indirect) | ✅ Safe |
| W2b | `aggregate_handover_create` | `inventory/views.py:483` | consumption | `fulfilled_issue`, `reference_task`/`reference_order_item` | P4 (indirect) | ✅ Safe |
| W6 | `execute_daily_delivery` | `inventory/services.py:1449` | consumption | *(none)* | n/a — this IS Engine B | ✅ Safe |
| W7 | `execute_daily_return` | `inventory/services.py:~1510` | return | *(none)* | n/a — return/adjustment only | ✅ Safe |
| W8 | `return_custody` | `inventory/services.py:454,466` | return / adjustment | *(none)* | n/a — correction only | ✅ Safe |
| W9 | `purchase_order_receive` | `inventory/views.py:1840` | purchase (inbound) | *(none)* | n/a — inbound | ✅ Safe |
| W10a | `raw_material_receive_scan` | `inventory/views.py:1966` | purchase (inbound) | *(none)* | n/a — inbound | ✅ Safe |
| W10b | `raw_material_receive_scan_api` | `inventory/views.py:2025` | purchase (inbound) | *(none)* | n/a — inbound | ✅ Safe |
| W11 | `issue_material` | `inventory/views.py:1310` | consumption | `fulfilled_issue`, `reference_task`/`reference_order_item` | P4 (indirect) | ✅ Safe |
| W12 | `stock_movement_create` | `inventory/views.py:1706` | **any (incl. consumption)** | **NONE** | ❌ **UNGUARDED** | ⚠️ **Open risk** |
| W13 | `scan_packaging_unit` (warehouse) | `product/views.py:3770` | consumption | `reference_task_id` | **P7** (line 1759) | ✅ Safe |

> **Non-writer views** (read-only, Phase 8–16): `daily_closing` (line 692), `material_ledger` (line 828), `consumption_report` (line 858), `material_dashboard` (line 911), `data_integrity_audit` (line 1041), `daily_queue_sources` (line 1058), `daily_queue_preview_delivery` (line 1101), `handover_preview` (line 346), `aggregate_handover_preview` (line 431), `custody_board` (line 510), `item_detail` (line 1399). None create StockMovement.

> **Signals:** `inventory/signals.py` only triggers queue sync (`request_queue_sync_for_tasks`). Does **not** create StockMovement.

> **Management commands (not runtime writers):** `backfill_material_issue_links.py` (PATCHES existing consumption movements by setting `fulfilled_issue_id`; does not create new ones), `report_paint_auto_consumption.py` (read/report + optional delete; does not create), `backfill_production_events.py` (calls `consume_material_for_task` only for non-paint done tasks), `seed_custody_demo.py` (creates `purchase` only), `consolidate_paint_material_issues.py:114` (creates MaterialIssue, not StockMovement).

---

## 3. Detailed Writer Analysis

### W1 — `production_issue_queue` "request" action (`inventory/views.py:178-205`)

- POST handler at line 178: `action == 'request'`
- **P4 guard** (line 183): `if task.station_name == 'paint'` → blocks MaterialIssue creation for paint tasks, redirects user to DailyMaterialQueue.
- Creates `MaterialIssue` via `get_or_create` (line 199) with `purpose='production'`, linked to `task`.
- Does NOT create StockMovement directly — the StockMovement comes later when the issue is handovered (W11).
- **Verdict:** Paint tasks cannot reach this path. ✅ Safe.

### W2 — `handover_create` (`inventory/views.py:361-400`)

- Accepts JSON payload of `[(issue_id, quantity)]` items.
- Calls `services.execute_handover(issued_by, items, ...)` → `services._create_issue_movement` → `StockMovement.objects.create(movement_type='consumption', ...)`.
- `_create_issue_movement` (`inventory/services.py:248-281`) creates consumption with:
  - `fulfilled_issue = issue` (always)
  - `reference_task = issue.task` (if `issue.task_id`), OR
  - `reference_order_item = item` (if `issue.order_item` is not None), OR `reference_color_part = issue.color_part`
- Issues in the handover are selected by the warehouse user from the `production_issue_queue` view (which already applies P4).
- **Verdict:** Relies on P4 preventing paint MaterialIssue creation. ✅ Safe.

### W2b — `aggregate_handover_create` (`inventory/views.py:477-501`)

- Same as W2: calls `services.execute_handover` with items distributed via `distribute_aggregate`.
- Same `_create_issue_movement` linkage.
- **Verdict:** Same as W2. ✅ Safe.

### W6 — `execute_daily_delivery` (`inventory/services.py:1385-1470`)

- Called from `daily_queue_delivery` view (`inventory/views.py:611`).
- Creates `StockMovement(movement_type='consumption', quantity=physical)` at line 1449.
- Sets `created_by` only — does NOT set `fulfilled_issue`, `reference_task`, or `reference_order_item`.
- The linkage is to the `DailyMaterialQueue` row (which holds `production_task`, `raw_material`, `worker`, etc.), not directly to the StockMovement.
- Idempotent: locks the queue row with `select_for_update`; blocks if `has_transaction` is already True (line 1416).
- This is the **canonical Engine B consumption path** — the authoritative source of truth when `NORMAL_PAINTING_ENGINE_IS_DAILY_QUEUE = True`.
- **Verdict:** This is the legitimate paint consumption path. Any other path creating consumption for the same material/quantity would double-count. ✅ Safe (by definition).

### W7 — `execute_daily_return` (`inventory/services.py:1474`)

- Called from `daily_queue_return` view (`inventory/views.py:654`).
- Creates `return` StockMovement (not consumption). Adjusts `returned_quantity`, `actual_consumption`, `status` on the queue row.
- **Verdict:** Return movement, not consumption. No double-count risk. ✅ Safe.

### W8 — `return_custody` (`inventory/services.py:386-502`)

- Called from `custody_return` view (`inventory/views.py:1180`).
- Creates `return` StockMovement (when `back > ZERO`, line 454) or `adjustment` StockMovement (when `consumed < ZERO`, line 466).
- Does NOT create consumption movement — the service docstring explicitly explains why (lines 403-407): "کل بستهٔ بازشده در لحظهٔ تحویل از انبار کسر شده و `current_stock` آن را «خارج‌شده» نشان می‌دهد. اگر اینجا دوباره حرکت مصرف سا�خته شود، موجودی **دو بار** کسر می‌شود."
- Consumption attribution goes to `CustodyConsumption` table (not StockMovement).
- **Verdict:** ✅ Safe.

### W9 — `purchase_order_receive` (`inventory/views.py:1827-1856`)

- Creates `StockMovement(movement_type='purchase', ...)` for inbound received goods.
- Auth: `@login_required @admin_or_manager_required`
- **Verdict:** Inbound. No consumption. ✅ Safe.

### W10a/W10b — `raw_material_receive_scan` / `raw_material_receive_scan_api` (`inventory/views.py:1930, 1997`)

- Creates `StockMovement(movement_type='purchase', ...)` for barcode-scanned inbound goods.
- Auth: `@login_required` (line 1930); `@login_required @warehouse_or_manager_required` (line 1997)
- **Verdict:** Inbound. No consumption. ✅ Safe.

### W11 — `issue_material` (`inventory/views.py:1299-1327`)

- Single-row handover: `services.execute_handover(items=[(issue_id, quantity)])` → `_create_issue_movement` → consumption StockMovement.
- Linked to the specific MaterialIssue via `fulfilled_issue`.
- MaterialIssue was created via `production_issue_queue` (W1), which has P4 guard.
- **Verdict:** ✅ Safe (same as W2).

### W12 — `stock_movement_create` (⚠️ **THE OPEN RISK**)

- **inventory/views.py:1688-1706**
- Creates `StockMovement` via `StockMovementForm`.
- `StockMovementForm` (`inventory/forms.py:39`) exposes only 6 fields: `raw_material`, `movement_type`, `quantity`, `unit_price`, `supplier`, `note`. **No** `task`, `order_item`, `color_part`, `fulfilled_issue`, or `reference_task` fields.
- The template (`inventory/templates/inventory/movements.html:88-96`) renders `movement_type` as a `<select>` with **all four choices**: `purchase`, `consumption`, `adjustment`, `return`. A user can select `consumption` for any material.
- The form validation (`inventory/forms.py`) validates quantity > 0 and `movement_type` is a valid choice — **no material-type check, no task/queue check**.
- Auth: `@login_required` + `@admin_or_manager_required` (line 1689-1690) — only admins/managers.
- **NO P7 guard** — there is no task reference to pass to `is_paint_need_delivered_by_queue`.

**Double-count scenario:** An admin creates a `consumption` StockMovement for raw material X (a paint material) via the "ثبت جدید" button on the movements page. If material X was already delivered to a paint worker via DailyMaterialQueue (W6 created a consumption StockMovement for the same physical quantity), the warehouse stock is decremented again — silently, with no reconciliation link.

**Why P7 is not present here:** The P7 guard (`is_paint_need_delivered_by_queue(task, raw)`) requires a `ProductionTask` reference. W12 has no task reference — the form is intentionally generic for arbitrary inventory bookkeeping. This is a structural limitation, not an oversight.

**Current DB evidence:** No `consumption` StockMovements exist (see Section 4). ✅ No active corruption.

### W13 — `scan_packaging_unit` warehouse branch (`product/views.py:3730-3776`)

- Warehouse-only branch (line 3731: `if is_warehouse_user(request.user):`)
- Creates `StockMovement(movement_type='consumption', reference_task_id=task_id)` at line 3770.
- **P7 guard** at lines 3759-3761:
  ```python
  if guard_task is not None and inventory_services.is_paint_need_delivered_by_queue(guard_task, raw):
      messages.error(request, 'نیاز نقاشی این تسک برای این ماده از صف مواد روزانه تحویل شده و بسته از انبار خارج شده؛ ثبت مصرف دستی آن موجودی را دوبار کم می‌کند...')
  ```
- The guard only fires when `task_id` is provided (i.e., the warehouse user selected a specific paint task). If no `task_id` is selected, the guard is bypassed (but the movement is then linked to `reference_task_id=None`, making it an unlinked consumption — see W12 overlap).
- **Verdict:** P7 guard is present when `task_id` is provided. ✅ Safe (with caveats noted below).

### W14 — `consume_material_for_task` (`product/utils.py:85-108`)

- Creates `StockMovement(movement_type='consumption', reference_task=task, fulfilled_issue=issue, note=...)` at line 85.
- Called ONLY when `self.station_name != 'paint'` (guard at `product/models.py:834`).
- This is the non-paint task completion hook.
- **Verdict:** ✅ Safe (non-paint only).

---

## 4. Live Database State (Read-Only SQLite Inspection)

All queries run read-only; `db.sqlite3` was not modified.

| Table | Row Count | Key Field Distribution | Notes |
|-------|-----------|----------------------|-------|
| `inventory_stockmovement` | **4** | 4 × `purchase`, 0 × `consumption`, 0 × `return`, 0 × `adjustment` | All inbound (purchase). No consumption movements exist. |
| `inventory_materialissue` | **15** | 13 × `requested`, 2 × `issued`, 0 × `cancelled` | All 13 `requested` issues are non-paint (purpose=`production`, task_id != NULL). 2 `issued` are also non-paint. |
| `inventory_materialcustody` | **0** | — | No custody records. |
| `inventory_dailymaterialqueue` | **65** | 65 × `pending`, 0 × `delivered`, 0 × `returned`, 0 × `closed`, 0 × `cancelled` | All pending. `delivered_quantity` = 0 for all. No deliveries yet. |
| `inventory_dailymaterialqueuesource` | **92** | — | Source rows for queue planning. |
| `product_painttask` / `productiontask` (station_name='paint') | **0** | — | No paint tasks exist in the database. |
| `product_packagingunit` | (checked) | — | Packaging units exist but no paint tasks linked. |
| `inventory_materialhandover` | (checked) | — | Handovers exist for non-paint issues only. |

**Key findings:**
1. **Zero consumption StockMovements** exist in the database — no double-counting has occurred.
2. **Zero paint ProductionTasks** exist — the paint/station_name='paint' pipeline has not been exercised in this DB.
3. All 15 MaterialIssue rows are non-paint (purpose=`production`, `task_id` is not null).
4. All 65 DailyMaterialQueue rows are `pending` with `delivered_quantity=0` — Engine B has not yet delivered anything.

---

## 5. Guard Chain Analysis (P4 / P5 / P7 / W14)

The normal-painting contract (Engine B delivery) is protected by a multi-layer guard chain:

| Layer | Location | Mechanism | Blocks Paint Consumption From |
|-------|----------|-----------|-------------------------------|
| **P4** | `inventory/views.py:183` | `if task.station_name == 'paint': return redirect(...)` | W1 (production_issue_queue — can't create paint MaterialIssue) |
| **P4** | `product/utils.py:531` (W3) | `cancel_unissued_paint_material_requests` deletes draft paint issues | W3 (cleanup of any stale paint MaterialIssue) |
| **P4** | `product/utils.py:2448` (W15) | `if NORMAL_PAINTING_ENGINE_IS_DAILY_QUEUE and is_paint_need_delivered_by_queue(task, raw): return` | W15 (auto_create_material_issues — skips paint MaterialIssue creation) |
| **P5** | `product/models.py:834` (W14) | `if self.station_name != 'paint': consume_material_for_task(...)` | W14 (consume_material_for_task — skips paint task auto-consumption) |
| **P7** | `product/views.py:1759` (W13) | `if is_paint_need_delivered_by_queue(guard_task, raw): messages.error(...)` | W13 (scan_packaging_unit warehouse — blocks manual consumption for delivered paint materials) |
| **Idempotency** | `inventory/services.py:1416` (W6) | `if queue.has_transaction or queue.status in ('delivered', 'returned', 'closed'): raise HandoverError` | W6 (execute_daily_delivery — prevents duplicate delivery) |

**The guard chain has a GAP:** W12 (`stock_movement_create`) has no guard. It can create `consumption` StockMovements for ANY raw material, including paint materials that are managed by Engine B, with NO task/issue/queue linkage.

---

## 6. Risk Assessment

### W12 Risk: Open — Double-Counted Consumption (Low Probability, Medium Impact)

**Threat model:** A user with `admin_or_manager_required` permission (auth level: admin) uses the "ثبت جدید" button on the movements page to create a `consumption` StockMovement for a paint material.

**Preconditions:**
1. User has admin/manager role (auth enforced)
2. A paint material exists with stock
3. Paint tasks using that material exist or are planned (or the user manually enters the material)
4. The material was already delivered (or concurrently delivered) via DailyMaterialQueue (W6)

**Impact if triggered:**
- Warehouse stock is decremented twice for the same physical material
- No reconciliation record links the manual consumption to any task or queue row
- The `material_ledger` and `data_integrity_audit` views would show discrepant stock
- Recovery would require manual investigation and a correcting `adjustment` movement (W12 itself)

**Probability:** Low — requires an admin user to deliberately create a consumption movement for a paint material that was already delivered via the queue. The UI dropdown shows material names with current stock, but does not indicate whether the material is paint-managed.

**Detectability:** Medium — `data_integrity_audit` view (`inventory/views.py:1041`) and `report_paint_auto_consumption` management command can identify orphaned consumption movements (those without `fulfilled_issue` link). However, W12 movements would have generic notes, not the paint-auto-consumption note prefix, so the report command would not flag them specifically.

### W13 Edge Case: Partial Guard (Negligible Risk)

W13's P7 guard only fires when `task_id` is provided (line 3756: `if task_id: guard_task = ...`). If a warehouse user creates a consumption movement for a paint material WITHOUT selecting a task_id, the guard does not fire, and the movement is created with `reference_task_id=None` — effectively the same unlinked consumption as W12.

**Mitigation:** This requires intentional omission of task_id. The form UI (`scan_material_issue.html`) makes task_id a visible select field.

---

## 7. Recommendations

### 7.1 (Recommended) Add Paint-Material Type Check to W12 (P17)

Since W12 has no task reference, the P7 predicate `is_paint_need_delivered_by_queue(task, raw)` cannot be used directly. Instead, add a simpler guard:

**At `inventory/views.py:1706` (inside `stock_movement_create`):**

Before saving the form, if `movement_type == 'consumption'`, check whether the selected `raw_material` is consumed by any paint-station task (via `PaintingMaterialRequirement` or `BOM` entries for paint). If so, reject the creation with: "این ماده برای مصرف در ایستگاه نقاشی است و باید از طریق صف مواد روزانه یا اسکن بسته‌بندی ثبت مصرف شود."

**Rationale:** The guard doesn't need a task reference — it only needs to identify the material as paint-managed. This is simpler than the P7 task-level check.

### 7.2 (Recommended) Add Orphan-Consumption Detection to Data Integrity Audit

Extend `reports.data_integrity_audit()` to flag `consumption` StockMovements where `fulfilled_issue IS NULL` AND `reference_task IS NULL` AND `reference_order_item IS NULL` — these are "orphan consumption" movements with no task linkage, which should not exist under the normal contract.

### 7.3 (Optional) Require `fulfilled_issue` for consumption movements in W12 form

Since `StockMovementForm` has no issue/task reference fields, W12 cannot create properly-linked consumption movements. Consider restricting the W12 form to only allow `purchase` and `adjustment` movement types, removing `consumption` and `return` from the user-facing dropdown. If consumption is needed, direct the warehouse user to W13 (`scan_packaging_unit`) or W11 (`issue_material`).

---

## 8. Conclusion

**P9-B decision.** The audit confirms that W12 (`stock_movement_create`) is the single remaining path that can create unguarded consumption StockMovements for paint materials delivered via Engine B. No such consumption movement currently exists in the database (all 4 StockMovement rows are inbound `purchase`). All other consumption paths are either Engine B (W6, which is the authoritative source) or properly guarded by P4/P5/P7/W14. The recommended fix is a paint-material type check in W12 (Section 7.1).

---

## 9. Verified File Inventory (Read-Only References)

| Path | Role |
|------|------|
| `inventory/models.py:89` | `StockMovement` model definition |
| `inventory/models.py:117` | `fulfilled_issue` FK field |
| `inventory/models.py:327` | `MaterialIssue` model |
| `inventory/models.py:434` | `DailyMaterialQueue` model |
| `inventory/forms.py:39` | `StockMovementForm` (6 fields, no task/issue refs) |
| `inventory/urls.py:27` | W12 URL: `movements/create/` → `movement_create` |
| `inventory/urls.py:30` | W11 URL: `production-queue/<id>/issue/` → `issue_material` |
| `inventory/views.py:174` | W1: `production_issue_queue` (P4 guard at line 183) |
| `inventory/views.py:346` | `handover_preview` (read-only) |
| `inventory/views.py:361` | W2: `handover_create` |
| `inventory/views.py:477` | W2b: `aggregate_handover_create` |
| `inventory/views.py:510` | `custody_board` (read-only) |
| `inventory/views.py:611` | W6: `daily_queue_delivery` → `execute_daily_delivery` |
| `inventory/views.py:654` | W7: `daily_queue_return` → `execute_daily_return` |
| `inventory/views.py:692` | `daily_closing` (read-only, Phase 8) |
| `inventory/views.py:828` | `material_ledger` (read-only, Phase 10) |
| `inventory/views.py:1041` | `data_integrity_audit` (read-only, Phase 16) |
| `inventory/views.py:1180` | W8: `custody_return` → `return_custody` |
| `inventory/views.py:1299` | W11: `issue_material` → `execute_handover` |
| `inventory/views.py:1688` | **W12: `stock_movement_create`** ← OPEN RISK |
| `inventory/views.py:1827` | W9: `purchase_order_receive` |
| `inventory/views.py:1930` | W10a: `raw_material_receive_scan` |
| `inventory/views.py:1997` | W10b: `raw_material_receive_scan_api` |
| `inventory/services.py:248` | `_create_issue_movement` (consumption linkage logic) |
| `inventory/services.py:285` | `execute_handover` (W2/W2b/W11) |
| `inventory/services.py:386` | `return_custody` (W8) |
| `inventory/services.py:1385` | `execute_daily_delivery` (W6) |
| `inventory/services.py:1474` | `execute_daily_return` (W7) |
| `inventory/services.py:1557` | `is_paint_need_delivered_by_queue` (P7 predicate) |
| `product/utils.py:85` | W14: `consume_material_for_task` (guarded at line 834) |
| `product/utils.py:2445` | `NORMAL_PAINTING_ENGINE_IS_DAILY_QUEUE = True` |
| `product/utils.py:2448` | W15: `auto_create_material_issues` (P4 guard) |
| `product/models.py:834` | W14 call site: `if self.station_name != 'paint'` |
| `product/models.py:615` | `STATION_CHOICES` (includes `paint`) |
| `product/views.py:3538` | W13: `scan_packaging_unit` (P7 guard at line 3759) |
| `product/views.py:1399` | `item_detail` (read-only) |
| `inventory/signals.py:40,78` | Queue sync only (no StockMovement creation) |
| `inventory/tests/test_p7_manual_consumption_guard.py:16,315` | P7 guard test (confirms W12 is intentionally unguarded) |
| `inventory/templates/inventory/movements.html:7` | W12 UI entry: "ثبت جدید" button |
| `inventory/templates/inventory/movements.html:88-96` | W12 form: `movement_type` dropdown with `consumption` option |
| `db.sqlite3` | Live database (4 StockMovement, 15 MaterialIssue, 65 DailyMaterialQueue) |
