"""Effective spec layer for the cutting/CNC workspace.

Operator edits never touch the base ``Part`` or ``ProductionTask`` records;
they land in ``TaskSpecOverride`` and every planning consumer (standard .CUT
planner, CNC sheet nesting, anomaly report) reads the part through this
module. Behaviour contract:

- No override row  ->  the effective spec is exactly ``Part``/``ProductionTask``
  (byte-for-byte the pre-workspace behaviour).
- ``None`` on an override field means «inherit from the base record».
- Tasks locked inside a planned cut or CNC program reject every edit until
  that program is cancelled (same fail-closed rule as stock reservations).
- All user-facing errors are Persian; the workspace API maps ``ValueError``
  to HTTP 400 verbatim.
"""
import hashlib
from collections import Counter
from dataclasses import dataclass, field as dc_field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q

from product.models import Material

from .models import CutPlanTask, TaskSpecOverride, TaskSpecOverrideEvent

MM = Decimal('0.1')
MAX_OVERRIDE_QUANTITY = 10000
EDGE_PART_FIELDS = {'f4': 'edge_f4', 'f5': 'edge_f5', 'f18': 'edge_f18', 'f26': 'edge_f26'}
OVERRIDE_FIELD_LABELS = {
    'length': 'طول', 'width': 'عرض', 'quantity': 'تعداد', 'material': 'متریال',
    'turn': 'چرخش', 'f4': 'نوار لبه F4', 'f5': 'نوار لبه F5',
    'f18': 'نوار لبه F18', 'f26': 'نوار لبه F26', 'excluded': 'خارج از برنامه',
}


class SpecLockedError(ValueError):
    """The task sits inside a planned program; edits are blocked until cancel."""


def _dimension(value, field_name):
    """Same rule as cutting.views._dimension: Decimal, > 0, one decimal max."""
    try:
        value = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError):
        raise ValueError(f'{field_name} باید عددی باشد.')
    if not value.is_finite() or value <= 0:
        raise ValueError(f'{field_name} باید بزرگ‌تر از صفر باشد.')
    if value.quantize(MM) != value:
        raise ValueError(f'{field_name} حداکثر یک رقم اعشار می‌پذیرد؛ مقدار بدون گردکردن ثبت نشد.')
    return value.quantize(MM)


def _planned_cut_rows(task_ids):
    """{task_id: {'quantity': n, 'plan_id': m}} for planned standard programs."""
    rows = {}
    if not task_ids:
        return rows
    query = CutPlanTask.objects.filter(plan__status='planned', task_id__in=task_ids)
    for row in query.values('task_id', 'quantity', 'plan_id'):
        entry = rows.setdefault(row['task_id'], {'quantity': 0, 'plan_id': row['plan_id']})
        entry['quantity'] += row['quantity']
    return rows


def _planned_cnc_rows(task_ids):
    """{task_id: {'pieces': n, 'plan_id': m}} for planned CNC sheet layouts."""
    rows = {}
    if not task_ids:
        return rows
    from cnc_resizer.models import CncSheetPlan
    wanted = set(task_ids)
    for plan in CncSheetPlan.objects.filter(status='planned').only('pk', 'layout'):
        for piece in (plan.layout or {}).get('parts', []):
            task_id = piece.get('task_id')
            if task_id is None:
                continue
            task_id = int(task_id)
            if task_id not in wanted:
                continue
            entry = rows.setdefault(task_id, {'pieces': 0, 'plan_id': plan.pk})
            entry['pieces'] += 1
    return rows


def plan_lock(task):
    """Return ``{'kind': 'cut'|'cnc', 'id': plan_pk}`` when the task is
    reserved by a still-planned program; ``None`` when it is editable."""
    kind_label = {'cut': 'برش', 'cnc': 'CNC'}
    cut_rows = _planned_cut_rows([task.pk])
    if task.pk in cut_rows:
        return {'kind': 'cut', 'id': cut_rows[task.pk]['plan_id'], 'label': kind_label['cut']}
    cnc_rows = _planned_cnc_rows([task.pk])
    if task.pk in cnc_rows:
        return {'kind': 'cnc', 'id': cnc_rows[task.pk]['plan_id'], 'label': kind_label['cnc']}
    return None


def lock_message(lock):
    return f"این قطعه در برنامهٔ {lock['label']} #{lock['id']} رزرو است؛ ابتدا آن برنامه را لغو کنید."


def _reserved_piece_count(task):
    """Pieces already committed to planned programs (defensive floor)."""
    total = _planned_cut_rows([task.pk]).get(task.pk, {}).get('quantity', 0)
    total += _planned_cnc_rows([task.pk]).get(task.pk, {}).get('pieces', 0)
    return total


def _is_cnc_first(part):
    from cnc_resizer.sheet_nesting import is_cnc_first
    return is_cnc_first(part)


def _open_manual_issue_task_ids(task_ids):
    from cnc_resizer.models import CncPlanningIssue
    if not task_ids:
        return set()
    return set(CncPlanningIssue.objects.filter(
        status='open', task_id__in=task_ids, details__stage='manual_required',
    ).values_list('task_id', flat=True))


@dataclass
class EffectiveSpec:
    """One editable row of the workspace parts table."""
    task_id: int
    part_id: int
    order_id: int | None
    order_number: str
    name: str                 # read-only display name (Part.name / f2)
    pname: str
    barcode: str              # Part.f3 (read-only, searchable)
    length: Decimal
    width: Decimal
    quantity_total: int
    quantity_completed: int
    quantity_remaining: int
    material_id: int
    material_name: str
    material_thickness: Decimal
    turn: bool
    grain: str
    edges: dict = dc_field(default_factory=dict)      # {'f4','f5','f18','f26'}
    excluded: bool = False
    excluded_reason: str = ''
    is_cnc_first: bool = False
    overridden_fields: set = dc_field(default_factory=set)
    reserved_plan: dict | None = None                 # {'kind','id','label'}
    version: int | None = None                        # override version, None = no overlay
    status_code: str = 'ready'
    routing_code: str = ''
    item_id: int | None = None                        # OrderItem link via task.order_item_id


def _override_fields(override):
    if override is None:
        return set()
    names = set()
    for name in ('length', 'width', 'quantity', 'material', 'turn'):
        if getattr(override, f'{name}_id' if name == 'material' else name) is not None:
            names.add(name)
    for part_field, override_field in EDGE_PART_FIELDS.items():
        if getattr(override, override_field) is not None:
            names.add(part_field)
    return names


def effective_spec(task, override=..., *, planned_cut=0, planned_cnc=0, manual_required=False,
                   reserved_plan=...):
    """Build the row for one task.

    ``override`` semantics: instance = use it; explicit ``None`` = definitely
    no overlay (avoids stale cached relations); default sentinel = read the
    (possibly cached) ``task.spec_override``. ``planned_*`` and
    ``manual_required`` let callers batch-prefetch; the default
    ``reserved_plan=...`` sentinel detects the lock for single-task calls.
    """
    part = task.part
    if override is ...:
        override = getattr(task, 'spec_override', None)
    if reserved_plan is ...:
        reserved_plan = plan_lock(task)
    length = override.length if override is not None and override.length is not None else part.length
    width = override.width if override is not None and override.width is not None else part.width
    quantity_total = override.quantity if override is not None and override.quantity is not None else task.quantity
    material = override.material if override is not None and override.material_id else part.material
    turn = override.turn if override is not None and override.turn is not None else bool(part.turn)
    edges = {}
    for part_field, override_field in EDGE_PART_FIELDS.items():
        value = getattr(override, override_field) if override is not None else None
        edges[part_field] = value if value is not None else (getattr(part, part_field) or '')
    excluded = bool(override and override.excluded)
    if excluded:
        quantity_remaining = 0
    else:
        quantity_remaining = max(0, int(quantity_total) - int(task.completed_quantity) - int(planned_cut) - int(planned_cnc))
    spec = EffectiveSpec(
        task_id=task.pk,
        part_id=part.pk,
        order_id=task.order_id,
        order_number=task.order.number if getattr(task, 'order', None) is not None else '',
        name=part.name,
        pname=part.pname,
        barcode=part.f3 or '',
        length=Decimal(length),
        width=Decimal(width),
        quantity_total=int(quantity_total),
        quantity_completed=int(task.completed_quantity),
        quantity_remaining=int(quantity_remaining),
        material_id=material.pk,
        material_name=material.name,
        material_thickness=material.thickness,
        turn=bool(turn),
        grain=part.grain or '',
        edges=edges,
        excluded=excluded,
        excluded_reason=(override.excluded_reason if override is not None else '') or '',
        is_cnc_first=_is_cnc_first(part),
        overridden_fields=_override_fields(override),
        reserved_plan=reserved_plan,
        version=override.version if override is not None else None,
        routing_code=part.routing_code or '',
        item_id=task.order_item_id,
    )
    spec.status_code = _status_code(spec, manual_required)
    return spec


def _status_code(spec, manual_required):
    if spec.excluded:
        return 'excluded'
    if spec.reserved_plan:
        return 'locked_in_plan'
    if spec.is_cnc_first:
        return 'needs_cnc_manual' if manual_required else 'needs_cnc'
    return 'ready'


STATUS_LABELS = {
    'ready': 'آمادهٔ چیدمان',
    'excluded': 'خارج از برنامه',
    'needs_cnc': 'نیازمند CNC',
    'needs_cnc_manual': 'نیازمند اصلاح دستی CNC',
    'auto_adjusted': 'اصلاح خودکار شد',
    'locked_in_plan': 'قفل (در برنامه)',
}


def effective_specs(tasks):
    """Batch resolver for a list of tasks (no N+1: three queries in total)."""
    task_ids = [task.pk for task in tasks]
    overrides = {
        row.task_id: row
        for row in TaskSpecOverride.objects.filter(task_id__in=task_ids).select_related('material')
    }
    cut_rows = _planned_cut_rows(task_ids)
    cnc_rows = _planned_cnc_rows(task_ids)
    manual_ids = _open_manual_issue_task_ids(task_ids)
    specs = []
    for task in tasks:
        override = overrides.get(task.pk)
        reserved_plan = None
        if task.pk in cut_rows:
            reserved_plan = {'kind': 'cut', 'id': cut_rows[task.pk]['plan_id'], 'label': 'برش'}
        elif task.pk in cnc_rows:
            reserved_plan = {'kind': 'cnc', 'id': cnc_rows[task.pk]['plan_id'], 'label': 'CNC'}
        specs.append(effective_spec(
            task, override,
            planned_cut=cut_rows.get(task.pk, {}).get('quantity', 0),
            planned_cnc=cnc_rows.get(task.pk, {}).get('pieces', 0),
            manual_required=task.pk in manual_ids,
            reserved_plan=reserved_plan,
        ))
    return specs


def spec_hash(spec):
    """Stable digest of the spec facts a plan depends on.

    Stored on ``CutPlanTask`` / inside the CNC layout at planning time and
    re-checked before results/download, so a silent spec drift can never
    validate an outdated program.
    """
    payload = '|'.join((
        'v1',
        format(Decimal(spec.length), 'f'),
        format(Decimal(spec.width), 'f'),
        str(int(spec.material_id)),
        '1' if spec.turn else '0',
        str(int(spec.quantity_total)),
        '1' if spec.excluded else '0',
    ))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]


def _format_value(value):
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, bool):
        return 'بله' if value else 'خیر'
    return str(value)


def _material_label(material):
    return f'{material.name} ({format(material.thickness, "f")}mm)' if material else ''


def _current_override_values(override):
    """Snapshot of the stored overlay values (None = inherit) for no-op tests."""
    if override is None:
        return {}
    values = {name: getattr(override, name) for name in ('length', 'width', 'quantity', 'turn')}
    values['material'] = override.material_id
    for part_field, override_field in EDGE_PART_FIELDS.items():
        values[part_field] = getattr(override, override_field)
    return values


def validate_override(task, fields):
    """Validate + normalize an operator edit against the task's current state.

    ``fields`` keys: length, width, quantity, material, turn, f4, f5, f18, f26.
    Returns ``{field: value}`` where a real change stores the new value and a
    deliberate return-to-base stores ``None`` (= inherit again). Restating the
    current value is a no-op and is dropped. Raises ``ValueError`` (or
    ``SpecLockedError``) with a Persian message.
    """
    part = task.part
    if part is None:
        raise ValueError('این تسک قطعه ندارد و از همین صفحه قابل ویرایش نیست.')
    lock = plan_lock(task)
    if lock:
        raise SpecLockedError(lock_message(lock))

    override = TaskSpecOverride.objects.filter(task=task).first()
    current = _current_override_values(override)
    normalized = {}

    def decide(name, base_value, new_value):
        """Return True when a real change is requested; record the plan."""
        here = current.get(name)
        if new_value == base_value:
            if here is None:
                return False                     # still inheriting — no-op
            normalized[name] = None              # deliberate return to base
            return True
        if here is not None and here == new_value:
            return False                         # same override restated
        normalized[name] = new_value
        return True

    if 'length' in fields and fields['length'] is not None:
        decide('length', Decimal(part.length), _dimension(fields['length'], 'طول'))
    if 'width' in fields and fields['width'] is not None:
        decide('width', Decimal(part.width), _dimension(fields['width'], 'عرض'))
    if 'quantity' in fields and fields['quantity'] is not None:
        try:
            quantity = int(str(fields['quantity']).strip())
        except (TypeError, ValueError):
            raise ValueError('تعداد باید عدد صحیح باشد.')
        if quantity < 0:
            raise ValueError('تعداد نمی‌تواند منفی باشد.')
        if quantity > MAX_OVERRIDE_QUANTITY:
            raise ValueError(f'تعداد حداکثر {MAX_OVERRIDE_QUANTITY} است.')
        floor = int(task.completed_quantity) + _reserved_piece_count(task)
        if quantity < floor:
            raise ValueError(f'تعداد نمی‌تواند از مجموع انجام‌شده و رزروشده ({floor}) کمتر باشد.')
        decide('quantity', int(task.quantity), quantity)
    if 'material' in fields and fields['material'] not in (None, ''):
        try:
            material_id = int(fields['material'])
        except (TypeError, ValueError):
            raise ValueError('متریال انتخابی معتبر نیست.')
        material = Material.objects.filter(pk=material_id).first()
        if material is None:
            raise ValueError('متریال انتخابی معتبر نیست.')
        if material_id != part.material_id:
            if _is_cnc_first(part) and material.thickness != part.material.thickness:
                raise ValueError(
                    'تغییر متریال قطعهٔ CNC فقط با ضخامت برابر مجاز است؛ '
                    f'ضخامت فعلی {format(part.material.thickness, "f")} و ضخامت متریال جدید {format(material.thickness, "f")} است.'
                )
        if decide('material', part.material_id, material.pk):
            normalized['material'] = None if normalized['material'] is None else material
    if 'turn' in fields and fields['turn'] is not None:
        decide('turn', bool(part.turn), bool(fields['turn']))
    for part_field in EDGE_PART_FIELDS:
        if part_field not in fields:
            continue
        raw = fields[part_field]
        if raw is None:
            continue
        decide(part_field, getattr(part, part_field) or '', str(raw).strip()[:100])

    # CNC geometry contract: length flows through resolve_cnc_file; width and
    # material changes are allowed to be *recorded* but stage-4 planning routes
    # such parts to the manual queue (fail-closed), never to silent auto-resize.
    return normalized


def _apply_changes(task, override, normalized, user, reason):
    events = []
    effective = effective_spec(task, override)
    model_field_map = {
        'length': 'length', 'width': 'width', 'quantity': 'quantity',
        'turn': 'turn', 'f4': 'edge_f4', 'f5': 'edge_f5',
        'f18': 'edge_f18', 'f26': 'edge_f26',
    }
    current_values = {
        'length': effective.length, 'width': effective.width,
        'quantity': effective.quantity_total, 'material': effective.material_id,
        'turn': effective.turn,
        'f4': effective.edges.get('f4', ''), 'f5': effective.edges.get('f5', ''),
        'f18': effective.edges.get('f18', ''), 'f26': effective.edges.get('f26', ''),
    }
    for name, new_value in normalized.items():
        old_value = current_values.get(name)
        if name == 'material':
            override.material_id = new_value.pk if new_value is not None else None
        else:
            setattr(override, model_field_map[name], new_value)  # None = inherit
        events.append(TaskSpecOverrideEvent.objects.create(
            task=task, field=name,
            old_value=_format_value(old_value)[:255],
            new_value='(پیش‌فرض)' if new_value is None else _format_value(new_value)[:255],
            reason=(reason or '')[:255], created_by=user,
        ))
    # Exclusion is a separate operator decision; a dimension edit must never
    # silently un-exclude a piece.
    override.version += 1
    override.updated_by = user
    override.save()
    return events


@transaction.atomic
def apply_override(task, fields, user, reason=''):
    """Validate then persist one operator edit; returns ``(spec, events)``.

    An edit that leaves the overlay empty deletes the override row entirely,
    so the task falls back to its base spec (also the «reset» path).
    """
    normalized = validate_override(task, fields)
    override = TaskSpecOverride.objects.select_for_update().filter(task=task).first()
    if override is None:
        override = TaskSpecOverride(task=task, version=0)
        # Re-validate under the row lock so two concurrent PATCHes cannot
        # both pass the planned-plan lock check and then both apply.
        normalized = validate_override(task, fields)
    if not normalized:
        spec = effective_spec(task, override if override.pk else None)
        return spec, []
    events = _apply_changes(task, override, normalized, user, reason)
    _cleanup_empty(override)
    return effective_spec(task, override), events


def _cleanup_empty(override):
    override.refresh_from_db()
    if override.is_empty():
        override.delete()


@transaction.atomic
def set_excluded(task, *, excluded, reason='', user=None):
    """«خارج از برنامه» — removes the piece from planning, never from the order.

    The base order/product records stay untouched; the exclusion is a flag on
    the overlay and is fully reversible via ``include``.
    """
    lock = plan_lock(task)
    if lock:
        raise SpecLockedError(lock_message(lock))
    excluded = bool(excluded)
    if excluded and not (reason or '').strip():
        raise ValueError('ثبت علت حذف از برنامه الزامی است (مثلاً «موجود در انبار» یا «قبلاً تولید شده»).')
    override = TaskSpecOverride.objects.select_for_update().filter(task=task).first()
    if override is None:
        override = TaskSpecOverride(task=task)
    effective = effective_spec(task, override)
    old_value = effective.excluded
    if old_value == excluded and (not excluded or override.excluded_reason == (reason or '').strip()):
        return effective_spec(task, override), []
    override.excluded = excluded
    override.excluded_reason = (reason or '').strip()[:255] if excluded else ''
    override.version += 1
    override.updated_by = user
    TaskSpecOverrideEvent.objects.create(
        task=task, field='excluded',
        old_value=_format_value(old_value), new_value=_format_value(excluded),
        reason=(reason or '')[:255], created_by=user,
    )
    override.save()
    _cleanup_empty(override)
    return effective_spec(task, override), []


@transaction.atomic
def reset_override(task, user):
    """Delete the overlay row (with an audit event) and return to base spec."""
    lock = plan_lock(task)
    if lock:
        raise SpecLockedError(lock_message(lock))
    override = TaskSpecOverride.objects.select_for_update().filter(task=task).first()
    if override is None:
        return effective_spec(task, None), []
    effective = effective_spec(task, override)
    TaskSpecOverrideEvent.objects.create(
        task=task, field='reset',
        old_value=f'نسخهٔ {override.version}',
        new_value='بازگشت به مشخصات پایه',
        created_by=user,
    )
    override.delete()
    return effective_spec(task, None), []


def workspace_task_queryset(order_ids):
    """Base queryset for the workspace parts table.

    Includes pending cut tasks that still need production (raw or overridden
    quantity) plus any task an operator has already touched via an override,
    so excluded/edited rows stay visible. Done tasks without an overlay are
    production history and are not listed.
    """
    from product.models import ProductionTask
    return ProductionTask.objects.filter(
        order_id__in=order_ids, station_name='cut', part__isnull=False,
    ).filter(
        Q(status='pending') | Q(spec_override__isnull=False),
    ).select_related('part__material', 'order', 'order__customer', 'spec_override__material')
