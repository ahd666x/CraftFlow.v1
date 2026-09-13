# product/migrations/0009_create_warehouse_group.py
from django.db import migrations


def create_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.get_or_create(name='انبار')


def reverse(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name='انبار').delete()


class Migration(migrations.Migration):
    dependencies = [('product', '0008_paintingprocessmaterial_is_color_variant_and_more')]
    operations = [migrations.RunPython(create_group, reverse)]