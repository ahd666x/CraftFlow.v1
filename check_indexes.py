import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'selvi.settings')
import django
django.setup()
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute('SELECT name FROM sqlite_master WHERE type="index" AND tbl_name="product_paintingmaterialrequirement"')
    for row in cursor.fetchall():
        print(row)