from django.db import migrations


def seed_existing_stock_events(apps, schema_editor):
    SheetStock = apps.get_model('cutting', 'SheetStock')
    SheetStockEvent = apps.get_model('cutting', 'SheetStockEvent')
    alias = schema_editor.connection.alias
    for stock in SheetStock.objects.using(alias).all().iterator():
        if SheetStockEvent.objects.using(alias).filter(stock_id=stock.pk).exists():
            continue
        SheetStockEvent.objects.using(alias).create(
            stock_id=stock.pk,
            material_id=stock.material_id,
            event_type='receive',
            quantity_delta=stock.quantity,
            width=stock.width,
            height=stock.height,
            is_offcut=stock.is_offcut,
            note='نقطه شروع تاریخچه؛ موجودی در نسخه قبلی ثبت شده بود.',
        )


class Migration(migrations.Migration):
    dependencies = [('cutting', '0002_cutplansheet_cutplantask_cuttingsettings_and_more')]
    operations = [migrations.RunPython(seed_existing_stock_events, migrations.RunPython.noop)]
