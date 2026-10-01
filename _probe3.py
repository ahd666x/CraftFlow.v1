import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'selvi.settings')
import django  # noqa: E402

django.setup()

from django.db import connection, reset_queries  # noqa: E402
from django.conf import settings  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from product.models import STATION_CHOICES, Order, ProductionTask  # noqa: E402

settings.DEBUG = True
VALID = [c for c, _ in STATION_CHOICES]

print('=== BLIND SPOT CHECK: station_load() excludes invalid station_name ===')
all_open = ProductionTask.objects.filter(status__in=['pending', 'waiting']).count()
valid_open = ProductionTask.objects.filter(
    status__in=['pending', 'waiting'], station_name__in=VALID).count()
print(f'  open tasks total              : {all_open}')
print(f'  open tasks with VALID station : {valid_open}')
print(f'  open tasks INVISIBLE to station_load(): {all_open - valid_open}')

print()
print('=== ProductionTask: open tasks by station, valid vs invalid ===')
for r in (ProductionTask.objects.filter(status__in=['pending', 'waiting'])
          .values('station_name').annotate(c=__import__('django').db.models.Count('id'))
          .order_by('-c')):
    mark = 'OK ' if r['station_name'] in VALID else 'XX '
    print(f"  {mark}{r['station_name']!r:16s} {r['c']:5d}")

print()
print('=== EXPLAIN QUERY PLAN: status-only filter (no index) ===')
qs = ProductionTask.objects.filter(status='pending')
with CaptureQueriesContext(connection) as ctx:
    list(qs)
for qd in ctx.captured_queries:
    print(' ', qd['sql'][:220])

print()
print('=== EXPLAIN QUERY PLAN (raw SQL) ===')
with connection.cursor() as cur:
    for sql in [
        "SELECT COUNT(*) FROM product_productiontask WHERE status='pending'",
        "SELECT COUNT(*) FROM product_productiontask WHERE station_name='mon' AND status='pending'",
        "SELECT COUNT(*) FROM product_productiontask WHERE order_item_id=5 AND station_name='paint' AND status='pending'",
        "SELECT station_name, COUNT(*) FROM product_productiontask GROUP BY station_name",
    ]:
        cur.execute('EXPLAIN QUERY PLAN ' + sql)
        print('  SQL:', sql[:95])
        for r in cur.fetchall():
            print('     ', r)

print()
print('=== TIMINGS on current DB (13,743 tasks) ===')
import time  # noqa: E402


def t(label, fn):
    s = time.perf_counter()
    r = fn()
    d = (time.perf_counter() - s) * 1000
    print(f'  {label:52s} {d:8.1f} ms  -> {r}')


from craftflow_ai.services import queries  # noqa: E402

t('queries.station_load()', lambda: queries.station_load()['open_tasks'])
t('queries.bottlenecks(limit=5)', lambda: len(queries.bottlenecks(limit=5)['bottlenecks']))
t('queries.production_status()', lambda: queries.production_status()['tasks']['open_total'])
t('queries.inventory_status(limit=200)', lambda: queries.inventory_status(limit=200)['total_materials'])
t('queries.delayed_orders(limit=200)', lambda: queries.delayed_orders(limit=200)['total_delayed'])
t('queries.order_details(130)', lambda: queries.order_details(130)['tasks_total'])
t('queries.material_shortages()', lambda: queries.material_shortages()['checked_issues'])
t('queries.order_material_requirements(130)', lambda: len(
    queries.order_material_requirements(130)['requested_from_bom']))
t('queries.quality_status()', lambda: queries.quality_status()['total_all'])
t('queries.production_report()', lambda: 'built')

print()
print('=== ORDER WITHOUT TASKS (release simulation candidate) ===')
with_tasks = Order.objects.filter(tasks__isnull=False).distinct().count()
print('  orders total              :', Order.objects.count())
print('  orders having >=1 task    :', with_tasks)
print('  orders with ZERO tasks    :', Order.objects.count() - with_tasks)
for o in Order.objects.filter(tasks__isnull=True)[:12]:
    items = o.items.count()
    has_bom = any(i.product.bom.exists() for i in o.items.all())
    print(f"    order {o.id:4d} status={o.status:10s} items={items} any_bom={has_bom}")

print()
print('=== N+1 RISK: _task_material_requirements over one order ===')
from inventory.views import _task_material_requirements  # noqa: E402
tasks = list(Order.objects.get(pk=130).tasks.filter(status__in=['pending', 'waiting'])
             .select_related('part__material__raw_material', 'painting_stage__process',
                             'order_item__product'))
print('  open tasks in order 130:', len(tasks))
with CaptureQueriesContext(connection) as ctx:
    for tk in tasks:
        try:
            _task_material_requirements(tk)
        except Exception:
            pass
print('  queries executed:', len(ctx.captured_queries))
print('  => O(tasks) queries:', len(ctx.captured_queries) > 1)