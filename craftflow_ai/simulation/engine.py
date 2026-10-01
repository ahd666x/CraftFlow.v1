"""
موتور شبیه‌سازی خالص (فاز ۲).

قرارداد سخت این ماژول:

* ورودی: ``(snapshot, scenario)`` — هر دو دادهٔ خالص.
* خروجی: ``projected_state`` / ``diff`` / ``evidence`` / ``confidence`` / ``limitations``.
* **هیچ نوشتنی در دیتابیس انجام نمی‌شود** و هیچ نمونهٔ مدل Django تغییر
  نمی‌کند. تمام محاسبات روی dict و dataclass انجام می‌شود.
* ریاضیات ``build_plans`` (باقی‌ماندهٔ سالن + گِرد کردن به بسته) **دوباره
  نوشته نمی‌شود**؛ همان تابع خالص ``inventory.services.build_plans`` با
  ورودی‌های duck-typed تغذیه می‌شود تا نتیجه با تحویل انبار یکسان بماند.
"""
from decimal import Decimal

from craftflow_ai.analysis.evidence import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SIGNAL_DERIVED,
    SIGNAL_MISSING,
    SIGNAL_REAL,
    Evidence,
    confidence_from_signals,
)
from craftflow_ai.analysis.limits import limitation_messages
from craftflow_ai.simulation.models import (
    Diff,
    Projection,
    QueueChange,
    QueueEntry,
    Scenario,
    SimulationResult,
    Snapshot,
    money,
    q2,
)

# ترتیب صف باز در CraftFlow: ابتدا priority (کمترین عدد = بالاترین اولویت)،
# سپس شناسهٔ سفارش. همان ترتیب ``craftflow_ai.services.queries.open_orders``.
QUEUE_SORT_KEY = 'priority_then_id'

# سقف تعداد ردیف جابه‌جایی گزارش‌شده؛ نتیجه باید برای context مدل کوچک بماند.
MAX_QUEUE_CHANGES = 50

SCENARIO_MATERIAL_AVAILABILITY = 'material_availability'
SCENARIO_ORDER_PRIORITY = 'order_priority'


# ----------------------------------------------------------------------
# آداپتور خواندنی برای build_plans
# ----------------------------------------------------------------------

class _PlanRaw:
    """
    نمای فقط‌خواندنی یک ماده برای ``build_plans``.

    عمداً یک کلاس ساده است، نه مدل Django: شبیه‌سازی نباید حتی بتواند
    موجودی واقعی را از طریق property دیتابیسی بخواند یا تغییر دهد.
    """

    __slots__ = ('pk', 'name', 'unit', 'pack_size', '_current_stock')

    def __init__(self, raw, current_stock=None):
        self.pk = raw.pk
        self.name = raw.name
        self.unit = raw.unit
        self.pack_size = raw.pack_size
        self._current_stock = q2(
            raw.current_stock if current_stock is None else current_stock
        )

    @property
    def current_stock(self):
        return self._current_stock

    def with_stock(self, extra):
        return _PlanRaw(self, q2(self._current_stock + Decimal(extra)))

    def get_unit_display(self):
        return self.unit


class _PlanIssue:
    """نمای فقط‌خواندنی یک درخواست مواد برای ``build_plans``."""

    __slots__ = ('pk', 'raw_material')

    def __init__(self, pk, raw):
        self.pk = pk
        self.raw_material = raw


# ----------------------------------------------------------------------
# سناریو: در دسترس بودن مواد
# ----------------------------------------------------------------------

def material_availability(snapshot: Snapshot, scenario: Scenario) -> SimulationResult:
    """
    «اگر X واحد به این ماده اضافه شود، وضعیت تحویل چه می‌شود؟»

    فقط سمت انبار. نیاز مواد BOM یک سفارش در فاز ۲ قابل محاسبه نیست
    (``missing_material_mapping``) و هرگز جعل نمی‌شود.
    """
    raw_id = scenario.get('raw_material_id')
    additional = q2(scenario.get('additional_quantity') or 0)

    if raw_id is None:
        return SimulationResult(
            scenario_kind=SCENARIO_MATERIAL_AVAILABILITY,
            status='insufficient_data',
            projected_state={},
            diff=Diff(()),
            confidence=CONFIDENCE_LOW,
            confidence_reasons=('سناریو raw_material_id ندارد.',),
            limitations=('missing_material_mapping',),
            no_effect_reason='missing_raw_material_id',
        )

    material = snapshot.material(raw_id)
    if material is None:
        return SimulationResult(
            scenario_kind=SCENARIO_MATERIAL_AVAILABILITY,
            status='insufficient_data',
            projected_state={},
            diff=Diff(()),
            confidence=CONFIDENCE_LOW,
            confidence_reasons=(
                f'مادهٔ اولیهٔ {raw_id} در snapshot وجود ندارد.',
            ),
            limitations=('missing_material_mapping',),
            no_effect_reason='raw_material_not_in_snapshot',
        )

    baseline = _plan_for(material, q2(material.raw.current_stock))
    projected_raw = _PlanRaw(material.raw).with_stock(additional)
    projected = _plan_for(material, projected_raw.current_stock)

    base_projection = _to_projection(material, baseline, material.raw.current_stock)
    new_projection = _to_projection(material, projected, projected_raw.current_stock)

    shortage_before = _shortage(base_projection)
    shortage_after = _shortage(new_projection)

    diff = Diff(tuple(_material_diff(base_projection, new_projection,
                                    additional, shortage_before, shortage_after)))

    signals = [SIGNAL_REAL]
    if not material.issues:
        signals.append(SIGNAL_DERIVED)
    confidence = confidence_from_signals(signals)

    limitations = ['missing_material_mapping']
    if not material.issues:
        limitations = limitations  # بدون درخواست باز، اثر سناریو فقط روی موجودی است

    evidence = [
        Evidence(
            metric='current_stock',
            value=money(material.raw.current_stock),
            unit=material.raw.unit,
            source='inventory.RawMaterial.current_stock (captured in snapshot)',
            query='stock derived from movements at snapshot time',
        ),
        Evidence(
            metric='additional_quantity',
            value=money(additional),
            unit=material.raw.unit,
            source='scenario parameter',
            query='the hypothetical incoming quantity supplied by the caller',
        ),
        Evidence(
            metric='projected_stock',
            value=money(projected_raw.current_stock),
            unit=material.raw.unit,
            source='snapshot stock + scenario additional_quantity',
            query='arithmetic on captured values only; no database write',
            derived=True,
        ),
        Evidence(
            metric='open_request_count',
            value=len(material.issues),
            unit='request',
            source='inventory.MaterialIssue (captured in snapshot)',
            query='open material issues of this raw material at snapshot time',
        ),
    ]

    if additional <= 0:
        no_effect = 'additional_quantity <= 0'
    elif material.issues:
        no_effect = None
    else:
        no_effect = 'no_open_requests_to_reallocate'

    return SimulationResult(
        scenario_kind=SCENARIO_MATERIAL_AVAILABILITY,
        status='complete',
        projected_state={
            'raw_material_id': material.raw.pk,
            'raw_material': material.raw.name,
            'unit': material.raw.unit,
            'current_stock': money(material.raw.current_stock),
            'additional_quantity': money(additional),
            'projected_stock': money(projected_raw.current_stock),
            'baseline': base_projection.to_dict(),
            'projected': new_projection.to_dict(),
            'sufficient_before': base_projection.enough,
            'sufficient_after': new_projection.enough,
            'shortage_before': money(shortage_before),
            'shortage_after': money(shortage_after),
            'open_request_count': len(material.issues),
            'plan_logic': 'inventory.services.build_plans (reused, not reimplemented)',
            'write_performed': False,
        },
        diff=diff,
        evidence=tuple(e.to_dict() for e in evidence),
        confidence=confidence,
        confidence_reasons=_material_confidence_reasons(confidence, len(material.issues)),
        limitations=limitations,
        affected_changes=(
            [new_projection.to_dict()] if material.issues else []
        ),
        no_effect_reason=no_effect,
    )


def _plan_for(material, stock):
    """
    اجرای همان ریاضیات واقعی تحویل انبار روی دادهٔ snapshot.

    از ``inventory.services.build_plans`` استفاده می‌شود تا بسته‌بندی و
    باقی‌ماندهٔ سالن دقیقاً همان چیزی باشد که انبار محاسبه می‌کند.
    """
    from inventory.services import build_plans

    raw = _PlanRaw(material.raw, current_stock=stock)
    issues = [
        (_PlanIssue(issue.pk, raw), issue.remaining())
        for issue in material.issues
    ]
    if not issues:
        return None
    plans = build_plans(issues, {material.raw.pk: material.leftover})
    return plans[0] if plans else None


def _to_projection(material, plan, projected_stock) -> Projection:
    if plan is None:
        return Projection(
            raw_material_id=material.raw.pk,
            name=material.raw.name,
            unit=material.raw.unit,
            needed=q2(0),
            from_leftover=q2(0),
            from_stock=q2(0),
            packs=0,
            pack_size=q2(material.raw.pack_size),
            physical_required=q2(0),
            leftover_after=q2(material.leftover),
            projected_stock=q2(projected_stock),
            enough=True,
        )
    return Projection(
        raw_material_id=plan['raw_material_id'],
        name=material.raw.name,
        unit=material.raw.unit,
        needed=q2(plan['need']),
        from_leftover=q2(plan['from_leftover']),
        from_stock=q2(plan['from_stock']),
        packs=int(plan['packs']),
        pack_size=q2(plan['pack_size']),
        physical_required=q2(plan['physical']),
        leftover_after=q2(plan['new_leftover']),
        projected_stock=q2(plan['stock']),
        enough=bool(plan['enough']),
    )


def _shortage(projection: Projection) -> Decimal:
    gap = q2(projection.projected_stock) - q2(projection.physical_required)
    return q2(gap) if gap < 0 else q2(0)


def _material_diff(before, after, additional, shortage_before, shortage_after):
    return (
        {
            'metric': 'stock',
            'before': money(before.projected_stock),
            'after': money(after.projected_stock),
            'delta': money(additional),
            'unit': after.unit,
        },
        {
            'metric': 'physical_required',
            'before': money(before.physical_required),
            'after': money(after.physical_required),
            'delta': money(q2(after.physical_required) - q2(before.physical_required)),
            'unit': after.unit,
        },
        {
            'metric': 'shortage',
            'before': money(shortage_before),
            'after': money(shortage_after),
            'delta': money(q2(shortage_after) - q2(shortage_before)),
            'unit': after.unit,
        },
        {
            'metric': 'sufficient',
            'before': before.enough,
            'after': after.enough,
            'delta': 'improved' if (after.enough and not before.enough) else 'unchanged',
            'unit': 'boolean',
        },
        {
            'metric': 'leftover_after',
            'before': money(before.leftover_after),
            'after': money(after.leftover_after),
            'delta': money(q2(after.leftover_after) - q2(before.leftover_after)),
            'unit': after.unit,
        },
    )


def _material_confidence_reasons(confidence, open_requests):
    if confidence == CONFIDENCE_LOW:
        return ['موجودی قابل محاسبه نبود.']
    reasons = ['موجودی و درخواست‌ها از snapshot دیتابیس آمده‌اند.']
    if not open_requests:
        reasons.append(
            'هیچ درخواست بازی برای این ماده وجود ندارد؛ اثر سناریو فقط روی '
            'موجودی است و تخصیص واقعی قابل سنجش نیست.'
        )
    if confidence == CONFIDENCE_MEDIUM:
        reasons.append('بخشی از دادهٔ تصمیم مشتق‌شده است.')
    return reasons


# ----------------------------------------------------------------------
# سناریو: اولویت سفارش
# ----------------------------------------------------------------------

def order_priority(snapshot: Snapshot, scenario: Scenario) -> SimulationResult:
    """
    «اگر اولویت این سفارش عوض شود، جای آن در صف کجا می‌شود؟»

    تنها اثر واقعی priority در CraftFlow **ترتیب صف** است. priority روی
    ``step_order`` اثر ندارد و CraftFlow برای تسک ``finish_time`` محاسبه
    نمی‌کند، پس هیچ زمانی تخمین زده نمی‌شود.
    """
    order_id = scenario.get('order_id')
    new_priority = scenario.get('new_priority')

    if order_id is None or new_priority is None:
        return SimulationResult(
            scenario_kind=SCENARIO_ORDER_PRIORITY,
            status='insufficient_data',
            projected_state={},
            diff=Diff(()),
            confidence=CONFIDENCE_LOW,
            confidence_reasons=('سناریو به order_id و new_priority نیاز دارد.',),
            limitations=('no_finish_time_model',),
            no_effect_reason='missing_scenario_parameters',
        )

    target = snapshot.order(order_id)
    if target is None:
        return SimulationResult(
            scenario_kind=SCENARIO_ORDER_PRIORITY,
            status='insufficient_data',
            projected_state={},
            diff=Diff(()),
            confidence=CONFIDENCE_LOW,
            confidence_reasons=(f'سفارش {order_id} در صف باز نیست.',),
            limitations=('no_finish_time_model',),
            no_effect_reason='order_not_in_open_queue',
        )

    before_queue = _sorted_queue(snapshot)
    after_queue = _sorted_queue(snapshot, overrides={int(order_id): int(new_priority)})

    changes = _queue_changes(target, int(new_priority), before_queue, after_queue)
    reported = changes[:MAX_QUEUE_CHANGES]
    truncated = len(changes) > MAX_QUEUE_CHANGES

    if int(new_priority) == int(target.priority):
        # هیچ تفاوتی با وضعیت فعلی وجود ندارد؛ صریحاً بی‌اثر گزارش می‌شود.
        status = 'no_change'
        no_effect = 'scenario_priority_equals_current_priority'
        confidence = CONFIDENCE_HIGH
        confidence_reasons = [
            'اولویت درخواستی با اولویت فعلی یکسان است، پس هیچ تغییری رخ نمی‌دهد.'
        ]
        reported = []
        truncated = False
    else:
        status = 'complete'
        no_effect = None
        signals = [SIGNAL_REAL]
        if len(snapshot.queue) < 2:
            signals.append(SIGNAL_DERIVED)
        confidence = confidence_from_signals(signals)
        confidence_reasons = [
            'ترتیب صف از دادهٔ واقعی باز سفارش‌ها و قاعدهٔ '
            '(priority, id) بازسازی شده است.'
        ]
        if len(snapshot.queue) < 2:
            confidence_reasons.append(
                'صف باز کمتر از دو سفارش دارد، پس جابه‌جایی قابل مشاهده نیست.'
            )

    evidence = [
        Evidence(
            metric='current_priority',
            value=target.priority,
            unit='priority_level',
            source='product.Order.priority',
            query='priority of this order at snapshot time',
        ),
        Evidence(
            metric='requested_priority',
            value=int(new_priority),
            unit='priority_level',
            source='scenario parameter',
            query='the hypothetical priority supplied by the caller',
        ),
        Evidence(
            metric='queue_position_before',
            value=_position_of(before_queue, int(order_id)),
            unit='position',
            source='craftflow_ai.services.queries.open_orders ordering',
            query='open orders sorted by (priority asc, id asc)',
            derived=True,
        ),
        Evidence(
            metric='queue_position_after',
            value=_position_of(after_queue, int(order_id)),
            unit='position',
            source='craftflow_ai.services.queries.open_orders ordering',
            query='same queue with the requested priority applied in memory only',
            derived=True,
        ),
        Evidence(
            metric='open_orders_in_queue',
            value=len(snapshot.queue),
            unit='order',
            source='product.Order (active statuses)',
            query='orders that are not completed',
        ),
    ]

    return SimulationResult(
        scenario_kind=SCENARIO_ORDER_PRIORITY,
        status=status,
        projected_state={
            'order_id': target.order_id,
            'order_number': target.number,
            'priority_before': target.priority,
            'priority_after': int(new_priority),
            'queue_position_before': _position_of(before_queue, int(order_id)),
            'queue_position_after': _position_of(after_queue, int(order_id)),
            'queue_order_before': [entry.order_id for entry in before_queue],
            'queue_order_after': [entry.order_id for entry in after_queue],
            'queue_order_changed': (
                [entry.order_id for entry in before_queue]
                != [entry.order_id for entry in after_queue]
            ),
            'moved_orders': len(changes),
            'moved_orders_reported': len(reported),
            'moved_orders_truncated': truncated,
            'effect': 'queue ordering changes',
            'not_affected': [
                'ProductionTask.step_order',
                'finish_time',
                'completion date',
            ],
            'write_performed': False,
        },
        diff=Diff(tuple(change.to_dict() for change in reported)),
        evidence=tuple(e.to_dict() for e in evidence),
        confidence=confidence,
        confidence_reasons=confidence_reasons,
        limitations=('no_finish_time_model',),
        affected_changes=[change.to_dict() for change in reported],
        no_effect_reason=no_effect,
    )


def _sorted_queue(snapshot: Snapshot, overrides=None):
    """صف باز طبق قاعدهٔ واقعی CraftFlow: priority صعودی، سپس id."""
    overrides = overrides or {}
    result = []
    for entry in snapshot.queue:
        override = overrides.get(entry.order_id)
        if override is not None and int(override) != int(entry.priority):
            entry = QueueEntry(
                order_id=entry.order_id,
                number=entry.number,
                priority=int(override),
                created_at=entry.created_at,
                open_tasks=entry.open_tasks,
            )
        result.append(entry)
    result.sort(key=lambda item: (item.priority, item.order_id))
    return result


def _position_of(queue, order_id):
    for index, entry in enumerate(queue, start=1):
        if entry.order_id == int(order_id):
            return index
    return None


def _queue_changes(target, new_priority, before_queue, after_queue):
    """سفارش‌هایی که واقعاً جابه‌جا شده‌اند (یا خودِ سفارش هدف)."""
    before_positions = {entry.order_id: index for index, entry in enumerate(before_queue, 1)}
    after_positions = {entry.order_id: index for index, entry in enumerate(after_queue, 1)}
    before_priority = {entry.order_id: entry.priority for entry in before_queue}
    after_priority = {entry.order_id: entry.priority for entry in after_queue}
    open_tasks = {entry.order_id: entry.open_tasks for entry in after_queue}
    numbers = {entry.order_id: entry.number for entry in after_queue}

    changes = []
    for order_id, position_after in sorted(after_positions.items()):
        position_before = before_positions.get(order_id)
        moved = position_before != position_after
        is_target = order_id == target.order_id
        if not moved and not is_target:
            continue
        changes.append(QueueChange(
            order_id=order_id,
            number=numbers.get(order_id),
            priority_before=before_priority.get(order_id, after_priority.get(order_id)),
            priority_after=after_priority.get(order_id, before_priority.get(order_id)),
            queue_position_before=position_before,
            queue_position_after=position_after,
            open_tasks=open_tasks.get(order_id, 0),
        ))
    return changes


def run(snapshot: Snapshot, scenario: Scenario) -> SimulationResult:
    """نقطهٔ ورود موتور — تنها یک تابع، بدون هیچ اثر جانبی."""
    if not isinstance(snapshot, Snapshot):
        raise TypeError('snapshot must be a Snapshot instance')
    if not isinstance(scenario, Scenario):
        raise TypeError('scenario must be a Scenario instance')

    if scenario.kind == SCENARIO_MATERIAL_AVAILABILITY:
        return material_availability(snapshot, scenario)
    if scenario.kind == SCENARIO_ORDER_PRIORITY:
        return order_priority(snapshot, scenario)

    return SimulationResult(
        scenario_kind=scenario.kind,
        status='insufficient_data',
        projected_state={},
        diff=Diff(()),
        confidence=CONFIDENCE_LOW,
        confidence_reasons=(f'سناریوی «{scenario.kind}» پشتیبانی نمی‌شود.',),
        limitations=('missing_dependency_graph',),
        no_effect_reason='unsupported_scenario',
    )


__all__ = [
    'MAX_QUEUE_CHANGES',
    'QUEUE_SORT_KEY',
    'SCENARIO_MATERIAL_AVAILABILITY',
    'SCENARIO_ORDER_PRIORITY',
    'limitation_messages',
    'material_availability',
    'order_priority',
    'run',
]