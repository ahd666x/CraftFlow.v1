import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'selvi.settings')
import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402
from django.conf import settings  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.db.models import Count  # noqa: E402
from product.models import (  # noqa: E402
    Order, ProductionTask, PaintingProcess, PaintingStage,
    PaintingMaterialRequirement, PaintingColorMaterialVariant,
    PaintingProcessMaterial, OrderItem,
)
from inventory.views import _task_material_requirements  # noqa: E402
from inventory.models import MaterialIssue, RawMaterial  # noqa: E402

settings.DEBUG = True

print('=== ORDERS THAT HAVE PAINT TASKS ===')
for o in Order.objects.filter(
        tasks__station_name='paint').distinct().order_by('-id')[:8]:
    pt = o.tasks.filter(station_name='paint')
    print(f"  order {o.id:4d} status={o.status:10s} paint_tasks={pt.count()} "
          f"with_stage={pt.filter(painting_stage__isnull=False).count()} "
          f"sched={pt.filter(scheduled_start__isnull=False).count()}")

paint_order = (Order.objects.filter(tasks__station_name='paint')
               .order_by('-id').first())
print(f'\n  chosen paint order: {paint_order.id}')

paint_tasks = list(paint_order.tasks.filter(station_name='paint').select_related(
    'painting_stage__process', 'order_item__product', 'part__material__raw_material'))

print()
print('=== PAINT TASKS DETAIL ===')
for t in paint_tasks[:14]:
    print(f"  task {t.id:6d} stage={t.status:8s} color_part={t.color_part!r:10s} "
          f"item={t.order_item_id} qty={t.quantity} stage_ref={t.painting_stage_id} "
          f"step={t.step_order} sched={bool(t.scheduled_start)} worker={t.assigned_worker_id}")

print()
print('=== N+1 TEST on paint order material requirements ===')
with CaptureQueriesContext(connection) as ctx:
    results = []
    for t in paint_tasks:
        try:
            results.append((t.id, _task_material_requirements(t)))
        except Exception as e:
            results.append((t.id, f'ERR {e}'))
print('  paint tasks:', len(paint_tasks), '| queries executed:', len(ctx.captured_queries))
print('  queries per task:', round(len(ctx.captured_queries) / max(len(paint_tasks), 1), 2))
for tid, rows in results[:6]:
    if isinstance(rows, list):
        print(f"    task {tid}: {[(r[0].name, str(r[1])) for r in rows]}")
    else:
        print(f"    task {tid}: {rows}")

print()
print('=== N+1 TEST on non-paint order (130) ===')
o130 = Order.objects.get(pk=130)
t130 = list(o130.tasks.filter(status__in=['pending', 'waiting']).select_related(
    'part__material__raw_material', 'painting_stage__process', 'order_item__product'))
with CaptureQueriesContext(connection) as ctx:
    for t in t130:
        try:
            _task_material_requirements(t)
        except Exception:
            pass
print('  open tasks:', len(t130), '| queries:', len(ctx.captured_queries))

print()
print('=== PAINTING PROCESS / REQUIREMENT RESOLUTION ===')
for p in PaintingProcess.objects.prefetch_related('stages').order_by('code'):
    print(f"  process code={p.code!r} name={p.name!r} colors={p.color_codes} active={p.is_active}")
    for s in p.stages.all():
        print(f"      stage {s.order}: {s.name!r} dur={s.duration_minutes} "
              f"dry={s.drying_time_minutes} skill={s.required_skill}")

print()
print('=== PaintingMaterialRequirement coverage by product/color_part ===')
rows = (PaintingMaterialRequirement.objects
        .values('product_id', 'product__name', 'color_part', 'process__code')
        .annotate(c=Count('id')))
print('  total requirement rows:', PaintingMaterialRequirement.objects.count())
print('  distinct (product, color_part) combos:', len({
    (r['product_id'], r['color_part']) for r in rows}))
print('  distinct products covered:',
      PaintingMaterialRequirement.objects.values('product_id').distinct().count(),
      'of', OrderItem.objects.values('product_id').distinct().count(), 'products used in orders')
print('  distinct color_parts:', sorted(
    {r['color_part'] for r in PaintingMaterialRequirement.objects.values('color_part')}))

print()
print('=== COLOR VARIANTS (is_color_variant slots) ===')
for pm in PaintingProcessMaterial.objects.prefetch_related('color_variants').order_by('process__code'):
    n = pm.color_variants.count()
    flag = 'COLOR-VARIANT' if pm.is_color_variant else 'fixed'
    print(f"  {pm.process.code:10s} {pm.raw_material.name:22s} {flag:14s} variants={n}")

print()
print('=== MaterialIssue linkage facts ===')
print('  issue with task       :', MaterialIssue.objects.filter(task__isnull=False).count())
print('  issue with order_item :', MaterialIssue.objects.filter(order_item__isnull=False).count())
print('  issue with painting_process:',
      MaterialIssue.objects.filter(painting_process__isnull=False).count())
print('  issue with packaging_unit  :',
      MaterialIssue.objects.filter(packaging_unit__isnull=False).count())
print('  issue with defect          :', MaterialIssue.objects.filter(defect__isnull=False).count())
for i in MaterialIssue.objects.all()[:10]:
    print(f"    issue {i.id} status={i.status:10s} purpose={i.purpose:10s} "
          f"raw={i.raw_material.name!r} task={i.task_id} item={i.order_item_id} "
          f"proc={i.painting_process_id} color_part={i.color_part!r} qty={i.requested_quantity}")

print()
print('=== RawMaterial fields (pack_size / min_stock_alert coverage) ===')
for r in RawMaterial.objects.all():
    print(f"  {r.name[:26]:28s} unit={r.unit:4s} pack_size={r.pack_size:>7} "
          f"min_alert={r.min_stock_alert:>8} stock={r.current_stock:>9} status={r.stock_status}")