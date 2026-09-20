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

SEED_RECORDS = [
    ('1',    '#efe9df', 'kham',    'سفید'),
    ('2',    '#ded2ba', 'kham',    'کرمی روشن'),
    ('3',    '#c9b896', 'kham',    'کرمی متوسط'),
    ('4',    '#b89968', 'kham',    'قهوه‌ای روشن'),
    ('5',    '#9c7b4f', 'kham',    'قهوه‌ای متوسط'),
    ('6',    '#7a5c3a', 'kham',    'قهوه‌ای تیره'),
    ('7',    '#5c4530', 'kham',    'قهوه‌ای خیلی تیره'),
    ('8',    '#3f3226', 'balot',   'مشکی مات'),
    ('9',    '#2b2119', 'gerdo',   'سفید مات'),
    ('10',   '#1a1512', 'gerdo',   'سفید سفت'),
    ('11',   '',       'balot',   'سایر'),
    ('جناغی','#' + '8a6a45', 'kham',    'جناغی'),
    ('بتنی', '#' + '9c9c94', 'botoni',  'بتنی'),
]


def seed_color_codes(apps, schema_editor):
    ColorCode = apps.get_model('product', 'ColorCode')
    for code, hex_code, material_name, description in SEED_RECORDS:
        ColorCode.objects.get_or_create(
            code=code,
            defaults={
                'hex_code': hex_code,
                'material_name': material_name,
                'description': description,
                'is_active': True,
            },
        )


def reverse_seed(apps, schema_editor):
    ColorCode = apps.get_model('product', 'ColorCode')
    codes = [r[0] for r in SEED_RECORDS]
    ColorCode.objects.filter(code__in=codes).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0013_add_colorcode_model'),
    ]

    operations = [
        migrations.RunPython(seed_color_codes, reverse_seed),
    ]
