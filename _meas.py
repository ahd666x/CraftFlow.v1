import django
django.setup()
from django.test import Client
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.contrib.auth.models import User
from product.models import (Order, OrderItem, Product, ProductCategory, PackagingUnit,
    ProductionLog, Customer, STATION_CHOICES)

u, _ = User.objects.get_or_create(username='measadmin', defaults={'is_superuser': True, 'is_staff': True})
if not u.is_superuser:
    u.is_superuser = True; u.is_staff = True; u.save()
u.set_password('pass'); u.save()
cat = ProductCategory.objects.first(); prod = Product.objects.first(); cust = Customer.objects.first()

items = []
for i in range(50):
    o = Order.objects.create(user=u, customer=cust, number='MEAS%d' % i)
    oi = OrderItem.objects.create(order=o, product=prod, quantity=3)
    items.append(oi)
    for stage, _ in STATION_CHOICES:
        if stage in ('packaging', 'shipping'):
            continue
        ProductionLog.objects.create(order_item=oi, stage=stage, user=u, notes='meas')
    for pu in oi.packaging_units.all():
        pu.is_packed = True; pu.packed_at = '2024-01-01T10:00:00Z'; pu.save(update_fields=['is_packed', 'packed_at'])
    last = oi.packaging_units.order_by('-unit_number').first()
    last.is_shipped = True; last.shipped_at = '2024-01-02T10:00:00Z'; last.save(update_fields=['is_shipped', 'shipped_at'])

c = Client(); ok = c.login(username='measadmin', password='pass')
print('LOGIN', ok)
with CaptureQueriesContext(connection) as cq:
    r = c.get('/reports/stages/')
print('STATUS', r.status_code)
print('NUM QUERIES:', len(cq))
for q in cq:
    print(q['sql'][:140].replace(chr(10), ' '))
