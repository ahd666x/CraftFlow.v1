import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'selvi.settings')
import django
django.setup()
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute("DELETE FROM django_migrations WHERE app='product' AND name IN ('0022_add_process_field_to_pmr', '0023_copy_process_from_stage')")
    print('Deleted migration records')