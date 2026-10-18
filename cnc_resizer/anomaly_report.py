from collections import defaultdict
from decimal import Decimal

from django.db.models import F

from cutting.models import SheetStock
from product.models import ProductionTask

DEFAULT_MAX_LENGTH_MM = Decimal('3000')
DEFAULT_MAX_ASPECT_RATIO = Decimal('8')
EDGE_MARGIN_MM = Decimal('10')


def _fits_on_stock(part, stock):
    usable_w = Decimal(stock.width) - EDGE_MARGIN_MM
    usable_h = Decimal(stock.height) - EDGE_MARGIN_MM
    length = Decimal(part.length)
    width = Decimal(part.width)
    # CUT sheet files use X = Part.length and Y = Part.width. The CNC
    # G-code generator has its own convention: X = geometry width, Y = CNC
    # length. This report checks CUT placement, so use the CUT convention.
    return (length <= usable_w and width <= usable_h) or (
        bool(part.turn) and width <= usable_w and length <= usable_h
    )


def find_unusual_parts(*, order_ids=None, task_ids=None, max_length=DEFAULT_MAX_LENGTH_MM,
                        max_aspect_ratio=DEFAULT_MAX_ASPECT_RATIO, include_done=False):
    """Return pending production parts that deserve operator review.

    The function never deletes, edits, or completes a task. It reports:
    - length above the configured absolute limit;
    - unusually high length/width ratio;
    - no active full sheet can contain the part, including the allowed turn.
    """
    filters = {'part__isnull': False, 'station_name': 'cut'}
    if not include_done:
        filters.update({'status': 'pending', 'quantity__gt': F('completed_quantity')})
    if order_ids is not None:
        filters['order_id__in'] = list(order_ids)
    if task_ids is not None:
        filters['id__in'] = list(task_ids)
    tasks = ProductionTask.objects.filter(**filters).select_related(
        'part__material', 'order', 'order__customer',
    ).order_by('order_id', 'part__name', 'pk')

    material_ids = {task.part.material_id for task in tasks}
    stock_by_material = defaultdict(list)
    for stock in SheetStock.objects.filter(
        material_id__in=material_ids, active=True, quantity__gt=0, is_offcut=False,
    ):
        stock_by_material[stock.material_id].append(stock)

    report = []
    for task in tasks:
        part = task.part
        length = Decimal(part.length)
        width = Decimal(part.width)
        reasons = []
        if length > max_length:
            reasons.append(f'طول {length} میلی‌متر از حد مجاز گزارش ({max_length}) بیشتر است')
        if width > 0 and max(length, width) / min(length, width) >= max_aspect_ratio:
            reasons.append(f'نسبت طول به عرض غیرمعمول است ({max(length, width) / min(length, width):.2f})')
        stocks = stock_by_material[part.material_id]
        if not stocks:
            reasons.append('برای متریال قطعه ورق کامل فعال ثبت نشده است')
        elif not any(_fits_on_stock(part, stock) for stock in stocks):
            reasons.append('روی هیچ‌یک از ورق‌های کامل فعال با چرخش مجاز جا نمی‌شود')
        if reasons:
            report.append({
                'task_id': task.id,
                'order_id': task.order_id,
                'part_id': part.id,
                'part_name': part.name,
                'barcode': part.f3,
                'material': part.material.name,
                'length': str(length),
                'width': str(width),
                'quantity_remaining': task.quantity - task.completed_quantity,
                'routing_code': part.routing_code or '',
                'reasons': reasons,
            })
    return report
