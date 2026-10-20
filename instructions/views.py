"""ویوهای اپ دستورالعمل‌ها — فاز ۱: سند «دستور تولید» قابل چاپ به ازای محصول."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, render

from product.models import Product

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

    grouped = {code: [] for code in WorkInstruction.STATION_ORDER}
    for ins in instructions:
        grouped.setdefault(ins.station, []).append(ins)

    sections = []
    for code in WorkInstruction.STATION_ORDER:
        items = grouped.get(code) or []
        if not items:
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
    }
    return render(request, 'instructions/product_doc.html', context)
