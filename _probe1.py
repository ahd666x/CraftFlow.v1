import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'selvi.settings')
import django  # noqa: E402

django.setup()

from django.db.models import Count, Q  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from product.models import (  # noqa: E402
    Order, OrderItem, ProductionTask, Part, Product, ProductBOM, Material,
    WorkerProfile, ProductionEvent, ProductionDefect, PackagingUnit, ShipmentLog,
    PaintingProcess, PaintingStage, PaintingMaterialRequirement,
    PaintingProcessMaterial, PaintingColorMaterialVariant, PaintingAssignmentRule,
    Holiday, ProductionLog, Customer,
)
from inventory.models import (  # noqa: E402
    RawMaterial, StockMovement, MaterialIssue, MaterialLeftover, MaterialCustody,
    MaterialCustodyReturn, MaterialHandover, MaterialHandoverLine,
    PurchaseOrder, Supplier,
)

MODELS = [
    ('Order', Order), ('OrderItem', OrderItem), ('ProductionTask', ProductionTask),
    ('Part', Part), ('Product', Product), ('ProductBOM', ProductBOM), ('Material', Material),
    ('WorkerProfile', WorkerProfile), ('ProductionEvent', ProductionEvent),
    ('ProductionDefect', ProductionDefect), ('PackagingUnit', PackagingUnit),
    ('ShipmentLog', ShipmentLog), ('PaintingProcess', PaintingProcess),
    ('PaintingStage', PaintingStage), ('PaintingMaterialRequirement', PaintingMaterialRequirement),
    ('PaintingProcessMaterial', PaintingProcessMaterial),
    ('PaintingColorMaterialVariant', PaintingColorMaterialVariant),
    ('PaintingAssignmentRule', PaintingAssignmentRule), ('Holiday', Holiday),
    ('ProductionLog', ProductionLog), ('Customer', Customer),
    ('RawMaterial', RawMaterial), ('StockMovement', StockMovement),
    ('MaterialIssue', MaterialIssue), ('MaterialLeftover', MaterialLeftover),
    ('MaterialCustody', MaterialCustody), ('MaterialCustodyReturn', MaterialCustodyReturn),
    ('MaterialHandover', MaterialHandover), ('MaterialHandoverLine', MaterialHandoverLine),
    ('PurchaseOrder', PurchaseOrder), ('Supplier', Supplier),
    ('User', User),
]

print('=== ROW COUNTS ===')
for name, model in MODELS:
    print(f'{name:36s} {model.objects.count():>8d}')

print()
print('=== TASK STATUS ===')
for row in ProductionTask.objects.values('status').annotate(c=Count('id')).order_by():
    print(f"  {row['status']:10s} {row['c']}")

print()
print('=== TASK STATION (open only) ===')
for row in (ProductionTask.objects.filter(status__in=['pending', 'waiting'])
            .values('station_name').annotate(c=Count('id')).order_by('-c')):
    print(f"  {row['station_name']:12s} {row['c']}")

print()
print('=== ORDER STATUS ===')
for row in Order.objects.values('status').annotate(c=Count('id')).order_by():
    print(f"  {row['status']:10s} {row['c']}")

print()
print('=== SCHEDULING FIELD POPULATION (ProductionTask) ===')
total = ProductionTask.objects.count()
print(f"  total tasks                      : {total}")
print(f"  scheduled_start IS NOT NULL      : {ProductionTask.objects.filter(scheduled_start__isnull=False).count()}")
print(f"  scheduled_end   IS NOT NULL      : {ProductionTask.objects.filter(scheduled_end__isnull=False).count()}")
print(f"  assigned_worker IS NOT NULL      : {ProductionTask.objects.filter(assigned_worker__isnull=False).count()}")
print(f"  both scheduled_start+end NOT NULL : {ProductionTask.objects.filter(scheduled_start__isnull=False, scheduled_end__isnull=False).count()}")
print(f"  painting_stage IS NOT NULL       : {ProductionTask.objects.filter(painting_stage__isnull=False).count()}")
print(f"  order IS NULL (custom card)      : {ProductionTask.objects.filter(order__isnull=True).count()}")
print(f"  part IS NULL                     : {ProductionTask.objects.filter(part__isnull=True).count()}")
print(f"  order_item IS NOT NULL           : {ProductionTask.objects.filter(order_item__isnull=True).count()}")
print(f"  completed_at IS NOT NULL         : {ProductionTask.objects.filter(completed_at__isnull=False).count()}")

print()
print('=== CUSTOM DURATION (the only per-task duration field) ===')
print(f"  custom_duration_minutes NOT NULL : {ProductionTask.objects.filter(custom_duration_minutes__isnull=False).count()}")
print(f"  custom_title NOT NULL             : {ProductionTask.objects.filter(custom_title__exclude(code='')).exclude(custom_title=None).count()}")

print()
print('=== WORKER PROFILES ===')
print(f"  profiles                          : {WorkerProfile.objects.count()}")
print(f"  is_available=True                 : {WorkerProfile.objects.filter(is_available=True).count()}")
from django.db.models import Sum  # noqa: E402
agg = WorkerProfile.objects.aggregate(
    s=Sum('__in_x') if False else None
) if False else None
print('  distinct work_start times         :',
      list(WorkerProfile.objects.values_list('work_start', flat=True).distinct()))
print('  distinct work_end times           :',
      list(WorkerProfile.objects.values_list('work_end', flat=True).distinct()))

print()
print('=== DUE DATE / PRIORITY COVERAGE (Order) ===')
print(f"  due_date NOT NULL                 : {Order.objects.filter(due_date__isnull=False).count()} / {Order.objects.count()}")
print(f"  priority distribution             :",
      {r['priority']: r['c'] for r in Order.objects.values('priority').annotate(c=Count('id')).order_by('priority')})

print()
print('=== MATERIAL ISSUE STATUS ===')
for row in MaterialIssue.objects.values('status').annotate(c=Count('id')).order_by():
    print(f"  {row['status']:10s} {row['c']}")
print('  purpose:', {r['purpose']: r['c'] for r in MaterialIssue.objects.values('purpose').annotate(c=Count('id'))})
print('  issue with task      :', MaterialIssue.objects.filter(task__isnull=False).count())
print('  issue with order_item:', MaterialIssue.objects.filter(order_item__isnull=False).count())
print('  issue with defect    :', MaterialIssue.objects.filter(defect__isnull=False).count())
print('  issue with pkg_unit  :', MaterialIssue.objects.filter(packaging_unit__isnull=False).count())

print()
print('=== DEFECT STATUS ===')
for row in ProductionDefect.objects.values('status').annotate(c=Count('id')).order_by():
    print(f"  {row['status']:20s} {row['c']}")
print('  defect w/ task     :', ProductionDefect.objects.filter(task__isnull=False).count())
print('  defect w/ pkg_unit :', ProductionDefect.objects.filter(packaging_unit__isnull=False).count())

print()
print('=== PACKAGING / SHIPPING ===')
print(f'  packaging units            : {PackagingUnit.objects.count()}')
print(f'  is_packed                  : {PackagingUnit.objects.filter(is_packed=True).count()}')
print(f'  is_shipped                 : {PackagingUnit.objects.filter(is_shipped=True).count()}')
print(f'  ShipmentLog                : {ShipmentLog.objects.count()}')

print()
print('=== PAINTING ===')
print(f'  processes                  : {PaintingProcess.objects.count()}')
print(f'  stages                     : {PaintingStage.objects.count()}')
print(f'  process_material (catalog) : {PaintingProcessMaterial.objects.count()}')
print(f'  material_requirements      : {PaintingMaterialRequirement.objects.count()}')
print(f'  color_variants             : {PaintingColorMaterialVariant.objects.count()}')
print(f'  assignment_rules           : {PaintingAssignmentRule.objects.count()}')
print(f'  paint tasks                : {ProductionTask.objects.filter(station_name="paint").count()}')
print('  painting processes detail  :')
for p in PaintingProcess.objects.prefetch_related('stages'):
    stages = ', '.join(f'{s.name}({s.duration_minutes}m/{s.drying_time_minutes}d/{s.required_skill})'
                       for s in p.stages.all())
    print(f'    {p.code:12s} colors={p.color_codes} stages=[{stages}]')

print()
print('=== STOCK MOVEMENT TYPES ===')
for row in StockMovement.objects.values('movement_type').annotate(c=Count('id')).order_by():
    print(f"  {row['movement_type']:12s} {row['c']}")

print()
print('=== DB / ENGINE ===')
from django.db import connection  # noqa: E402
print('  engine:', connection.vendor)
print('  sqlite version:', connection.Database.sqlite_version)
with connection.cursor() as cur:
    cur.execute("PRAGMA page_size")
    print('  page_size:', cur.fetchone()[0])
    cur.execute("PRAGMA journal_mode")
    print('  journal_mode:', cur.fetchone()[0])
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name LIKE 'product_%'")
    print('  product indexes:', [r[0] for r in cur.fetchall()])