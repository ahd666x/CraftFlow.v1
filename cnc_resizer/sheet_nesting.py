import math
from collections import Counter, defaultdict
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F

from cutting.models import CutPlanSheet, CutPlanTask, SheetStock, SheetStockEvent
from cutting.optimizer import optimize
from product.models import ProductionTask
from .models import CncPlanningIssue, CncSheetPlan
from .services import CncResolutionError, resolve_cnc_file

from . import gcode_sheet


def _ceil_tenth(value):
    """Round a measured size UP to 0.1 mm so nested parts can never overlap."""
    return Decimal(str(math.ceil(round(value * 10, 6)) / 10))


def is_cnc_first(part):
    stations = [x.strip().lower() for x in (part.routing_code or '').split('.') if x.strip()]
    return bool(stations and stations[0] == 'cnc')


def _with_explicit_mm(text):
    """Return ``(text, injected)`` with ``G21`` pinned before the first X/Y word.

    For post-processors that never emit a modal unit word (G20/G21 absent).
    A program that already declares G21 — or declares inches (G20, which the
    caller rejects) — is returned unchanged. The injected line shifts no
    geometry; it only makes the millimetre assumption explicit and machine
    independent, so every downstream consumer (sheet output included) sees
    an unambiguous program.
    """
    lines = text.splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        clean = gcode_sheet._clean(raw_line)
        words = gcode_sheet._words(clean)
        codes = [
            int(value) for letter, value, _raw in words
            if letter == 'G' and value.is_integer()
        ]
        if 20 in codes or 21 in codes:
            return text, False
        if any(letter in ('X', 'Y') for letter, _value, _raw in words):
            newline = '\r\n' if raw_line.endswith('\r\n') else '\n'
            lines.insert(index, 'G21' + newline)
            return ''.join(lines), True
    return text, False


def _source_geometry(path):
    """Read a part program and measure its *cutting* geometry (arcs included)."""
    with open(path, 'r', encoding='utf-8-sig') as stream:
        text = stream.read()
    implicit_mm = str(getattr(settings, 'CNC_RESIZER_IMPLICIT_UNITS', '') or '').strip().lower() == 'mm'
    if implicit_mm:
        text, units_injected = _with_explicit_mm(text)
    else:
        units_injected = False
    unit_mode = None
    distance_mode = None
    for raw_line in text.splitlines():
        clean = gcode_sheet._clean(raw_line)
        codes = [
            int(value) for letter, value, _raw in gcode_sheet._words(clean)
            if letter == 'G' and value.is_integer()
        ]
        if 20 in codes:
            raise gcode_sheet.GcodeError('فایل منبع شامل واحد اینچ (G20) است؛ تبدیل به ابعاد میلی‌متری رد شد.')
        if 91 in codes:
            raise gcode_sheet.GcodeError('فایل منبع شامل مختصات نسبی (G91) است؛ اندازه‌گیری هندسه رد شد.')
        if 21 in codes:
            unit_mode = 'mm'
        if 90 in codes:
            distance_mode = 'absolute'
        has_xy = any(letter in ('X', 'Y') for letter, _value, _raw in gcode_sheet._words(clean))
        if has_xy and 'G53' not in clean:
            if unit_mode != 'mm':
                raise gcode_sheet.GcodeError(
                    'واحد فایل منبع باید پیش از نخستین حرکت صریحاً میلی‌متر (G21) باشد؛ '
                    'اگر پست‌پروسسور شما هرگز واحد نمی‌نویسد و قطعاً میلی‌متر است، '
                    'CNC_RESIZER_IMPLICIT_UNITS=mm را در تنظیمات فعال کنید.'
                )
            if distance_mode != 'absolute':
                raise gcode_sheet.GcodeError('مختصات فایل منبع باید پیش از نخستین حرکت صریحاً مطلق (G90) باشد.')
    _pre, body, _post = gcode_sheet.split_program(text)
    min_x, min_y, max_x, max_y = gcode_sheet.source_bounds(body)
    return text, units_injected, min_x, min_y, max_x, max_y


def _planned_piece_counts():
    """task_id -> pieces already placed on a still-planned CNC sheet."""
    counts = Counter()
    for layout in CncSheetPlan.objects.filter(status='planned').values_list('layout', flat=True):
        for piece in (layout or {}).get('parts', []):
            if piece.get('task_id') is not None:
                counts[int(piece['task_id'])] += 1
    return counts


def _cut_reserved_counts(stock_ids):
    """Sheets already reserved by the standard (.CUT) planner."""
    counts = Counter()
    for stock_id in CutPlanSheet.objects.filter(
        outcome='reserved', plan__status='planned', source_stock_id__in=stock_ids,
    ).values_list('source_stock_id', flat=True):
        counts[stock_id] += 1
    return counts


@transaction.atomic
def create_cnc_sheet_plans(order_ids, user, kerf=Decimal('4.0')):
    """Nest CNC-first parts into saved sheet layouts without guessing a controller.

    Executable output remains blocked until a verified controller-specific sheet
    adapter is implemented; this function records the layout and reservation only.
    """
    tasks = list(ProductionTask.objects.select_for_update().filter(
        order_id__in=order_ids, station_name='cut', status='pending', part__isnull=False,
        quantity__gt=F('completed_quantity'),
    ).exclude(
        pk__in=CutPlanTask.objects.filter(plan__status='planned').values('task_id')
    ).select_related('part__material', 'order').order_by('pk'))
    tasks = [t for t in tasks if is_cnc_first(t.part)]
    if not tasks:
        return [], []

    grouped = defaultdict(list)
    issues = []

    def register_issue(task, reason, **details):
        reason_text = str(reason)[:10000]
        stage = details.get('stage')
        issue = CncPlanningIssue.objects.filter(
            task=task, mode='cnc_sheets', status='open', reason=reason_text,
            details__stage=stage,
        ).order_by('-pk').first()
        if issue:
            old_quantity = int((issue.details or {}).get('quantity', 0) or 0)
            new_quantity = int(details.get('quantity', 0) or 0)
            if new_quantity:
                details['quantity'] = max(old_quantity, new_quantity) if stage == 'source_or_geometry' else old_quantity + new_quantity
            issue.details = {**(issue.details or {}), **details}
            issue.save(update_fields=('details',))
        else:
            issue = CncPlanningIssue.objects.create(
                task=task, order=task.order, part=task.part, mode='cnc_sheets',
                reason=reason_text, details=details, created_by=user,
            )
        if all(existing.pk != issue.pk for existing in issues):
            issues.append(issue)

    already_planned = _planned_piece_counts()
    for task in tasks:
        pending = task.quantity - task.completed_quantity - already_planned.get(task.id, 0)
        pending = max(0, pending)
        if not pending:
            continue
        try:
            record = resolve_cnc_file(task.part, user)
            text, units_injected, min_x, min_y, max_x, max_y = _source_geometry(record.path)
        except (CncResolutionError, OSError, gcode_sheet.GcodeError, ValueError) as exc:
            register_issue(task, exc, stage='source_or_geometry', quantity=pending)
            continue
        for _ in range(pending):
            grouped[task.part.material_id].append({
                'task_id': task.id, 'part_id': task.part_id, 'label': task.part.f2 or task.part.name or task.part.f3,
                'f3': task.part.f3, 'width': _ceil_tenth(max_x - min_x),
                'height': _ceil_tenth(max_y - min_y), 'text': text, 'units_injected': units_injected,
                'min_x': min_x, 'min_y': min_y, 'max_x': max_x, 'max_y': max_y,
                'turn': bool(task.part.turn), 'order_id': task.order_id,
                'task_quantity': task.quantity, 'completed_before': task.completed_quantity,
            })

    result = []
    for material_id, pieces in grouped.items():
        stock_rows = list(SheetStock.objects.select_for_update().filter(
            material_id=material_id, active=True, quantity__gt=0, is_offcut=False,
        ).select_related('material').order_by('priority', 'width', 'height', 'pk'))
        if not stock_rows:
            for piece in pieces:
                task = next(task for task in tasks if task.id == piece['task_id'])
                register_issue(task, 'برای متریال قطعه ورق کامل آزاد پیدا نشد.', stage='stock')
            continue
        available = []
        cut_reserved = _cut_reserved_counts([s.pk for s in stock_rows])
        for stock in stock_rows:
            used = CncSheetPlan.objects.filter(source_stock=stock, status='planned').count() + cut_reserved[stock.pk]
            available.extend([stock] * max(0, stock.quantity - used))
        if not available:
            for piece in pieces:
                task = next(task for task in tasks if task.id == piece['task_id'])
                register_issue(task, 'همهٔ ورق‌های کامل این متریال در برنامه‌های دیگر رزرو شده‌اند.', stage='stock')
            continue
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
        for layout in layouts:
            stock = SheetStock.objects.get(pk=layout['sheet']['stock_id'])
            last = CncSheetPlan.objects.filter(source_stock=stock, created_by=user).order_by('-sheet_index').first()
            sheet_index = (last.sheet_index + 1) if last else 0
            output_parts = []
            for placed in layout['parts']:
                p = pieces[placed['uid']]
                rotated = bool(placed.get('rotated'))
                output_parts.append({'task_id': p['task_id'], 'part_id': p['part_id'], 'label': p['label'],
                                     'f3': p['f3'], 'order_id': p['order_id'],
                                     'x': str(placed['x']), 'y': str(placed['y']),
                                     'width': str(placed['width']), 'height': str(placed['height']),
                                     'rotated': rotated, 'units_injected': p['units_injected'],
                                     'task_quantity': p['task_quantity'],
                                     'completed_before': p['completed_before']})
            task_piece_counts = Counter(piece['task_id'] for piece in output_parts)
            for task_id, quantity in task_piece_counts.items():
                task = next(task for task in tasks if task.id == task_id)
                register_issue(
                    task,
                    'خروجی دستگاه تولید نشد: کنترلر مقصد و پست‌پروسسور مخصوص ورق در مخزن تأیید نشده است.',
                    stage='output_blocked', quantity=quantity,
                )
            plan = CncSheetPlan.objects.create(
                material=stock.material, source_stock=stock, sheet_index=sheet_index,
                width=stock.width, height=stock.height, kerf=kerf,
                planned_area=sum((Decimal(piece['width']) * Decimal(piece['height']) for piece in output_parts), Decimal('0')),
                layout={'parts': output_parts, 'material_id': material_id},
                gcode='', output_verified=False, created_by=user,
            )
            SheetStockEvent.objects.create(
                stock=stock, material=stock.material, cnc_plan=plan, event_type='reserve',
                quantity_delta=0, width=stock.width, height=stock.height, is_offcut=False,
                note=f'رزرو در برنامه CNC #{plan.pk}', created_by=user,
            )
            result.append(plan)
    return result, issues
