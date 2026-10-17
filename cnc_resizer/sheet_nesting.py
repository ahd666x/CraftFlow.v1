import re
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import F

from cutting.models import SheetStock, CutPlanTask
from cutting.optimizer import optimize
from product.models import ProductionTask
from .models import CncPlanningIssue, CncSheetPlan
from .services import resolve_cnc_file

NUM_RE = r"[+-]?\d+(?:\.\d+)?"
AXIS_RE = re.compile(r"([XYIJ])(" + NUM_RE + r")")


def is_cnc_first(part):
    stations = [x.strip().lower() for x in (part.routing_code or '').split('.') if x.strip()]
    return bool(stations and stations[0] == 'cnc')


def _transform_gcode(text, dx, dy, rotate=False, source_width=0, source_height=0, source_min_x=0, source_min_y=0):
    """Translate one part program into sheet coordinates; preserve feeds/depths."""
    def repl(match):
        axis, raw = match.group(1), match.group(2)
        value = float(raw)
        if rotate:
            if axis == 'X': value = source_height - (value - source_min_y)
            elif axis == 'Y': value = value - source_min_x
            elif axis == 'I': value = -value
            elif axis == 'J': value = value
        if axis == 'X': value += float(dx)
        elif axis == 'Y': value += float(dy)
        return axis + (f'{value:.3f}'.rstrip('0').rstrip('.') if value % 1 else str(int(value)))

    lines = []
    for line in text.splitlines():
        stripped = line.strip().upper()
        if stripped in ('%', 'M2', 'M30') or stripped.startswith('O'):
            continue
        lines.append(AXIS_RE.sub(repl, line))
    return '\n'.join(lines)


def _source_geometry(path):
    """Bounding box of the *part* geometry, excluding machine reference moves.

    A G-code file ends with a ``G53X0Y3950`` style return to the sheet origin.
    Counting those coordinates inflated every part's Y span to 3950 mm, which
    made the sheet placer think even a 300 mm strip was 3.95 m long.
    """
    with open(path, 'r', encoding='utf-8-sig') as stream:
        text = stream.read()
    points = []
    for line in text.splitlines():
        stripped = line.strip().upper()
        if stripped in ('%', 'M2', 'M30') or stripped.startswith('O') or 'G53' in stripped:
            continue
        xs = re.findall(r'X(' + NUM_RE + r')', line, re.I)
        ys = re.findall(r'Y(' + NUM_RE + r')', line, re.I)
        if xs or ys:
            points.append((float(xs[-1]) if xs else None, float(ys[-1]) if ys else None))
    xs = [p[0] for p in points if p[0] is not None]
    ys = [p[1] for p in points if p[1] is not None]
    if not xs or not ys:
        raise ValueError('فایل CNC مختصات X/Y قابل استفاده ندارد.')
    return text, min(xs), min(ys), max(xs), max(ys)


def create_cnc_sheet_plans(order_ids, user, kerf=Decimal('4.0')):
    """Create one G-code program per available SheetStock sheet for CNC-first parts."""
    tasks = list(ProductionTask.objects.filter(
        order_id__in=order_ids, station_name='cut', status='pending', part__isnull=False,
        quantity__gt=F('completed_quantity'),
    ).select_related('part__material', 'order'))
    tasks = [t for t in tasks if is_cnc_first(t.part)]
    if not tasks:
        return [], []

    grouped = defaultdict(list)
    issues = []

    def register_issue(task, reason, **details):
        issue = CncPlanningIssue.objects.create(
            task=task, order=task.order, part=task.part, mode='cnc_sheets',
            reason=str(reason)[:10000], details=details, created_by=user,
        )
        issues.append(issue)

    for task in tasks:
        for _ in range(task.quantity - task.completed_quantity):
            try:
                record = resolve_cnc_file(task.part, user)
                text, min_x, min_y, max_x, max_y = _source_geometry(record.path)
                grouped[task.part.material_id].append({
                    'task_id': task.id, 'part_id': task.part_id, 'label': task.part.f2 or task.part.name or task.part.f3,
                    'f3': task.part.f3, 'width': Decimal(str(max_x - min_x)),
                    'height': Decimal(str(max_y - min_y)), 'text': text,
                    'min_x': min_x, 'min_y': min_y, 'max_x': max_x, 'max_y': max_y,
                    'turn': bool(task.part.turn), 'order_id': task.order_id,
                })
            except Exception as exc:
                register_issue(task, exc, stage='source_or_geometry')

    result = []
    for material_id, pieces in grouped.items():
        stock_rows = list(SheetStock.objects.filter(
            material_id=material_id, active=True, quantity__gt=0, is_offcut=False,
        ).select_related('material').order_by('priority', 'width', 'height', 'pk'))
        if not stock_rows:
            for piece in pieces:
                task = next(task for task in tasks if task.id == piece['task_id'])
                register_issue(task, 'برای متریال قطعه ورق کامل آزاد پیدا نشد.', stage='stock')
            continue
        sheets = []
        available = []
        for stock in stock_rows:
            used = CncSheetPlan.objects.filter(source_stock=stock, status='planned').count()
            available.extend([stock] * max(0, stock.quantity - used))
        bins = [{'stock': stock, 'parts': []} for stock in available]
        # Reuse the deterministic cutting shelf optimizer for rectangle placement.
        optimizer_parts = [{**p, 'orig_width': p['width'], 'orig_height': p['height'], 'turn': p['turn'], 'uid': i} for i, p in enumerate(pieces)]
        stock_data = [{'stock_id': b['stock'].id, 'width': b['stock'].width, 'height': b['stock'].height,
                       'is_offcut': False, 'priority': b['stock'].priority, 'grain': b['stock'].grain,
                       'label': str(b['stock'])} for b in bins]
        try:
            layouts = optimize(optimizer_parts, stock_data, kerf, policy='priority')
        except Exception as exc:
            # Keep feasible pieces and isolate pieces that prevent the batch
            # from being laid out. A bad piece must not discard good output.
            remaining = list(optimizer_parts)
            layouts = []
            while remaining:
                try:
                    layouts = optimize(remaining, stock_data, kerf, policy='priority')
                    break
                except Exception as nested_exc:
                    message = str(nested_exc)
                    candidate = next((item for item in remaining if item['label'] in message), remaining[-1])
                    remaining.remove(candidate)
                    task = next(task for task in tasks if task.id == candidate['task_id'])
                    register_issue(task, nested_exc, stage='nesting')
            if not layouts and remaining:
                for candidate in remaining:
                    task = next(task for task in tasks if task.id == candidate['task_id'])
                    register_issue(task, exc, stage='nesting')
        for sheet_index, layout in enumerate(layouts):
            stock = SheetStock.objects.get(pk=layout['sheet']['stock_id'])
            commands = ['%', 'G21', 'G90', f'; CraftFlow CNC sheet {material_id}-{sheet_index + 1}']
            output_parts = []
            for placed in layout['parts']:
                p = pieces[placed['uid']]
                rotated = bool(placed.get('rotated'))
                if rotated:
                    dx = Decimal(str(placed['x'])) + Decimal('5')
                    dy = Decimal(str(placed['y'])) + Decimal('5')
                else:
                    dx = Decimal(str(placed['x'])) - Decimal(str(p['min_x'])) + Decimal('5')
                    dy = Decimal(str(placed['y'])) - Decimal(str(p['min_y'])) + Decimal('5')
                commands.append(f'; PART {p["f3"]} ORDER {p["order_id"]} TASK {p["task_id"]}')
                commands.append(_transform_gcode(
                    p['text'], dx, dy, rotate=rotated,
                    source_width=float(p['width']), source_height=float(p['height']),
                    source_min_x=float(p['min_x']), source_min_y=float(p['min_y']),
                ))
                output_parts.append({'task_id': p['task_id'], 'part_id': p['part_id'], 'label': p['label'],
                                     'f3': p['f3'], 'x': str(placed['x']), 'y': str(placed['y']),
                                     'width': str(placed['width']), 'height': str(placed['height']),
                                     'rotated': rotated})
            commands.extend(['M30', '%'])
            plan = CncSheetPlan.objects.create(
                material=stock.material, source_stock=stock, sheet_index=sheet_index,
                width=stock.width, height=stock.height, kerf=kerf,
                layout={'parts': output_parts, 'material_id': material_id},
                gcode='\n'.join(commands) + '\n', created_by=user,
            )
            result.append(plan)
    return result, issues
