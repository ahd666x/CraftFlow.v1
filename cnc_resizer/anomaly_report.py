from collections import defaultdict
from decimal import Decimal

from django.db.models import F, Q

from cutting.models import SheetStock
from product.models import ProductionTask

DEFAULT_MAX_LENGTH_MM = Decimal('3000')
DEFAULT_MAX_ASPECT_RATIO = Decimal('8')
EDGE_MARGIN_MM = Decimal('10')


def _fits_on_stock(length, width, turn, stock):
    usable_w = Decimal(stock.width) - EDGE_MARGIN_MM
    usable_h = Decimal(stock.height) - EDGE_MARGIN_MM
    # CUT sheet files use X = Part.length and Y = Part.width. The CNC
    # G-code generator has its own convention: X = geometry width, Y = CNC
    # length. This report checks CUT placement, so use the CUT convention.
    return (length <= usable_w and width <= usable_h) or (
        bool(turn) and width <= usable_w and length <= usable_h
    )


def find_unusual_parts(*, order_ids=None, task_ids=None, max_length=DEFAULT_MAX_LENGTH_MM,
                        max_aspect_ratio=DEFAULT_MAX_ASPECT_RATIO, include_done=False):
    """Return pending production parts that deserve operator review.

    The function never deletes, edits, or completes a task. Dimensions, turn
    and remaining quantity come from the *effective* spec (operator overrides
    included), so an overridden piece is validated against real stock:
    - length above the configured absolute limit;
    - unusually high length/width ratio;
    - no active full sheet can contain the part, including the allowed turn.
    """
    filters = {'part__isnull': False, 'station_name': 'cut'}
    if not include_done:
        filters['status'] = 'pending'
    if order_ids is not None:
        filters['order_id__in'] = list(order_ids)
    if task_ids is not None:
        filters['id__in'] = list(task_ids)
    query = ProductionTask.objects.filter(**filters)
    if not include_done:
        query = query.filter(
            Q(quantity__gt=F('completed_quantity')) | Q(spec_override__quantity__gt=F('completed_quantity')),
        ).exclude(spec_override__excluded=True)
    tasks = query.select_related(
        'part__material', 'order', 'order__customer', 'spec_override__material',
    ).order_by('order_id', 'part__name', 'pk')

    from cutting.specs import effective_specs
    specs_by_task = {spec.task_id: spec for spec in effective_specs(list(tasks))}

    material_ids = {spec.material_id for spec in specs_by_task.values()}
    stock_by_material = defaultdict(list)
    for stock in SheetStock.objects.filter(
        material_id__in=material_ids, active=True, quantity__gt=0, is_offcut=False,
    ):
        stock_by_material[stock.material_id].append(stock)

    report = []
    for task in tasks:
        spec = specs_by_task[task.pk]
        length = Decimal(spec.length)
        width = Decimal(spec.width)
        reasons = []
        if length > max_length:
            reasons.append(f'طول {length} میلی‌متر از حد مجاز گزارش ({max_length}) بیشتر است')
        if width > 0 and max(length, width) / min(length, width) >= max_aspect_ratio:
            reasons.append(f'نسبت طول به عرض غیرمعمول است ({max(length, width) / min(length, width):.2f})')
        stocks = stock_by_material[spec.material_id]
        if not stocks:
            reasons.append('برای متریال قطعه ورق کامل فعال ثبت نشده است')
        elif not any(_fits_on_stock(length, width, spec.turn, stock) for stock in stocks):
            reasons.append('روی هیچ‌یک از ورق‌های کامل فعال با چرخش مجاز جا نمی‌شود')
        if reasons:
            report.append({
                'task_id': task.id,
                'order_id': task.order_id,
                'part_id': task.part_id,
                'part_name': spec.name,
                'barcode': spec.barcode,
                'material': spec.material_name,
                'length': str(length),
                'width': str(width),
                'quantity_remaining': spec.quantity_remaining,
                'routing_code': spec.routing_code,
                'reasons': reasons,
            })
    return report
