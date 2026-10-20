"""ویوهای اپ دستورالعمل‌ها — سند «دستور تولید» قابل چاپ به ازای محصول.

فاز ۲: بخش خودکار ایستگاه CNC (پارکینگ ابزار، نقشه‌ی میز با نقاط فیکس،
روند آفست) از راه‌اندازی ماشین به سند اضافه می‌شود.
"""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, render

from product.models import Product

from .cnc_guide import get_active_setup, serialize_setup
from .models import WorkInstruction


def _is_staff(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


@login_required
@user_passes_test(_is_staff)
def product_doc(request, pk):
    """سند گردآوری‌شده‌ی دستور تولید محصول: ایستگاه‌ها به ترتیب مسیر تولید.

    این همان قالب عمومی محصول است؛ نسخه‌ی «به ازای سفارش» (با متریال و رنگ
    واقعی و اعمال شرط‌ها) در فاز بعدی از همین ساختار استفاده می‌کند.
    در قالب محصول همه‌ی دستورالعمل‌های فعال نشان داده می‌شوند و شرط‌های
    متریال/رنگ به‌صورت برچسب کنار هر بند مشخص‌اند.
    """
    product = get_object_or_404(
        Product.objects.select_related('category'), pk=pk,
    )
    instructions = (WorkInstruction.objects
                    .active()
                    .filter(product=product)
                    .prefetch_related('images', 'condition_material', 'condition_color'))
    cnc_setup = _cnc_doc_block()

    grouped = {code: [] for code in WorkInstruction.STATION_ORDER}
    for ins in instructions:
        grouped.setdefault(ins.station, []).append(ins)

    sections = []
    for code in WorkInstruction.STATION_ORDER:
        items = grouped.get(code) or []
        auto = (code == 'cnc' and cnc_setup is not None)
        if not items and not auto:
            continue
        sections.append({
            'code': code,
            'label': dict(WorkInstruction.STATION_FILTER_CHOICES).get(code, code),
            'items': items,
        })

    bom_rows = []
    for row in product.bom.select_related('part__material').order_by('id'):
        part = row.part
        bom_rows.append({
            'name': part.name or part.f2 or part.f3 or '—',
            'material': part.material.name if part.material_id else '—',
            'size': f"{part.length}×{part.width}",
            'edges': [(label, getattr(part, field) or '—')
                      for label, field in (('F26', 'f26'), ('F18', 'f18'), ('F4', 'f4'), ('F5', 'f5'))],
            'routing': part.routing_code,
            'quantity': row.quantity,
        })

    context = {
        'product': product,
        'sections': sections,
        'bom_rows': bom_rows,
        'has_instructions': bool(sections),
        'cnc_setup': cnc_setup,
    }
    return render(request, 'instructions/product_doc.html', context)


def _cnc_doc_block():
    """بخش خودکار ایستگاه CNC برای سند چاپی؛ None یعنی راه‌اندازی ثبت نشده."""
    setup = get_active_setup()
    if setup is None:
        return None
    payload = serialize_setup(setup)
    tw, th = float(setup.table_width), float(setup.table_height)
    aspect = th / tw if tw else 1

    def place(x, y):
        return {
            'left': round(x / tw * 100, 2) if tw else 0,
            'top': round((th - y) / th * 100, 2) if th else 0,
        }

    points = []
    for point in payload['points']:
        pos = place(point['x'], point['y'])
        points.append({
            'label': point['label'],
            'kind_label': point['kind_label'],
            'left': pos['left'], 'top': pos['top'],
        })
    origin = place(setup.origin_x, setup.origin_y)
    payload['map'] = {'points': points, 'origin': origin, 'aspect': round(aspect, 4)}
    return payload
