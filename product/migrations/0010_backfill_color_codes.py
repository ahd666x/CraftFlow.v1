from django.db import migrations

DEFAULT_HEX_MAP = {
    '1': '#efe9df', '2': '#ded2ba', '3': '#c9b896', '4': '#b89968',
    '5': '#9c7b4f', '6': '#7a5c3a', '7': '#5c4530', '8': '#3f3226',
    '9': '#2b2119', '10': '#1a1512',
    'جناغی': '#8a6a45', 'بتنی': '#9c9c94',
}
DEFAULT_MATERIAL_MAP = {
    '1': 'kham', '2': 'kham', '3': 'kham', '4': 'kham', '5': 'kham',
    '6': 'kham', '7': 'kham', '8': 'balot', '9': 'gerdo', '10': 'gerdo',
    'بتنی': 'botoni', 'جناغی': 'kham', '11': 'balot',
}


def backfill_color_codes(apps, schema_editor):
    Color = apps.get_model('product', 'Color')
    updates = []
    for color in Color.objects.all():
        code = str(color.code)
        hex_code = DEFAULT_HEX_MAP.get(code, '')
        material_name = DEFAULT_MATERIAL_MAP.get(code, '')
        if color.hex_code != hex_code or color.material_name != material_name:
            updates.append((color.pk, hex_code, material_name))
    if updates:
        for pk, hex_code, material_name in updates:
            Color.objects.filter(pk=pk).update(hex_code=hex_code, material_name=material_name)


def reverse_backfill(apps, schema_editor):
    Color = apps.get_model('product', 'Color')
    Color.objects.update(hex_code='', material_name='')


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0009_color_hex_code_material_name'),
    ]

    operations = [
        migrations.RunPython(backfill_color_codes, reverse_backfill),
    ]
