import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'selvi.settings')
import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402
from django.db.models import Count, Q, Sum  # noqa: E402
from product.models import (  # noqa: E402
    STATION_CHOICES, Order, ProductionTask, Part, WorkerProfile,
    PaintingProcess, PaintingStage, PaintingMaterialRequirement,
    PaintingProcessMaterial, PaintingColorMaterialVariant,
    ProductionDefect, PackagingUnit, Holiday,
)
from inventory.models import (  # noqa: E402
    RawMaterial, StockMovement, MaterialIssue, MaterialCustody, MaterialLeftover,
)

VALID = {c for c, _ in STATION_CHOICES}

print('=== STATION_CHOICES (valid codes) ===')
print(' ', sorted(VALID))

print()
print('=== station_name VALUES NOT IN STATION_CHOICES ===')
bad = (ProductionTask.objects
       .exclude(station_name__in=VALID)
       .values('station_name')
       .annotate(c=Count('id'), orders=Count('order', distinct=True),
                 done=Count('id', filter=Q(status='done')))
       .order_by('-c'))
bad_total = 0
for r in bad:
    bad_total += r['c']
    print(f"  {r['station_name']!r:16s} tasks={r['c']:5d} orders={r['orders']:4d} done={r['done']:5d}")
print(f'  TOTAL tasks with invalid station_name: {bad_total}')

print()
print('=== routing_code sample (source of station names) ===')
for r in Part.objects.exclude(routing_code='').values('routing_code').annotate(c=Count('id')).order_by('-c')[:20]:
    print(f"  {r['routing_code']!r:30s} parts={r['c']}")

print()
print('=== DISTINCT station_name count ===')
print('  distinct station_name values in DB:',
      ProductionTask.objects.values('station_name').distinct().count())
print('  valid codes with at least one task:',
      ProductionTask.objects.filter(station_name__in=VALID).values('station_name').distinct().count())

print()
print('=== CUSTOM DURATION ===')
print('  custom_duration_minutes NOT NULL :',
      ProductionTask.objects.filter(custom_duration_minutes__isnull=False).count())
print('  custom_title NOT NULL             :',
      ProductionTask.objects.filter(Q(custom_title='') | Q(custom_title__isnull=True)).exclude(
          Q(custom_title='') | Q(custom_title__isnull=True)).count())

print()
print('=== WORKER PROFILES ===')
print('  profiles:', WorkerProfile.objects.count(),
      '| available:', WorkerProfile.objects.filter(is_available=True).count())
print('  distinct work_start:', sorted({str(t) for t in WorkerProfile.objects.values_list('work_start', flat=True)}))
print('  distinct work_end  :', sorted({str(t) for t in WorkerProfile.objects.values_list('work_end', flat=True)}))
print('  distinct break     :', sorted({str(t) for t in WorkerProfile.objects.values_list('break_start', flat=True)}))
print('  distinct stages    :',
      {r['stage']: r['c'] for r in WorkerProfile.objects.values('stage').annotate(c=Count('id'))})
print('  skills present     :', [w.skills for w in WorkerProfile.objects.all()][:15])

print()
print('=== ORDER due_date / priority ===')
print('  due_date NOT NULL:', Order.objects.filter(due_date__isnull=False).count(), '/', Order.objects.count())
print('  priority dist    :',
      {r['priority']: r['c'] for r in Order.objects.values('priority').annotate(c=Count('id')).order_by('priority')})

print()
print('=== HOLIDAYS ===')
print('  Holiday rows:', Holiday.objects.count())

print()
print('=== MATERIAL ISSUE / DEFECT / PAINTING ===')
print('  MaterialIssue:', MaterialIssue.objects.count(),
      dict((r['status'], r['c']) for r in MaterialIssue.objects.values('status').annotate(c=Count('id'))))
print('  issue.purpose :', dict((r['purpose'], r['c']) for r in MaterialIssue.objects.values('purpose').annotate(c=Count('id'))))
print('  ProductionDefect rows:', ProductionDefect.objects.count())
print('  paint tasks   :', ProductionTask.objects.filter(station_name='paint').count())
print('  paint tasks w/ scheduled_start:',
      ProductionTask.objects.filter(station_name='paint', scheduled_start__isnull=False).count())
print('  paint tasks w/ painting_stage:',
      ProductionTask.objects.filter(station_name='paint', painting_stage__isnull=False).count())
print('  paint tasks w/ BOTH stage+sched:',
      ProductionTask.objects.filter(station_name='paint', painting_stage__isnull=False,
                                     scheduled_start__isnull=False).count())
print('  painting processes:', PaintingProcess.objects.count(),
      '| stages:', PaintingStage.objects.count())
print('  PaintingMaterialRequirement:', PaintingMaterialRequirement.objects.count())
print('  PaintingProcessMaterial    :', PaintingProcessMaterial.objects.count())
print('  PaintingColorMaterialVariant:', PaintingColorMaterialVariant.objects.count())
print('  scheduled tasks at non-paint stations:',
      ProductionTask.objects.exclude(station_name='paint').filter(scheduled_start__isnull=False).count())

print()
print('=== PACKAGING ===')
print('  units:', PackagingUnit.objects.count(),
      '| packed:', PackagingUnit.objects.filter(is_packed=True).count(),
      '| shipped:', PackagingUnit.objects.filter(is_shipped=True).count())

print()
print('=== LEFTOVER / CUSTODY ===')
print('  MaterialLeftover rows:', MaterialLeftover.objects.count(),
      'total qty:', MaterialLeftover.objects.aggregate(t=Sum('quantity'))['t'])
print('  MaterialCustody rows :', MaterialCustody.objects.count(),
      'total qty:', MaterialCustody.objects.aggregate(t=Sum('quantity'))['t'])
print('  StockMovement rows   :', StockMovement.objects.count())

print()
print('=== SQLITE ENGINE FACTS ===')
print('  vendor:', connection.vendor)
with connection.cursor() as cur:
    cur.execute('PRAGMA page_size')
    print('  page_size:', cur.fetchone()[0])
    cur.execute('PRAGMA journal_mode')
    print('  journal_mode:', cur.fetchone()[0])
    cur.execute('PRAGMA cache_size')
    print('  cache_size:', cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM sqlite_master WHERE type='index'")
    print('  total indexes:', cur.fetchone()[0])
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='product_productiontask'")
    print('  productiontask indexes:', [r[0] for r in cur.fetchall()])
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='product_productiontask'")
    print('  productiontask DDL:')
    print('   ', (cur.fetchone()[0] or '').replace('\n', ' ')[:600])