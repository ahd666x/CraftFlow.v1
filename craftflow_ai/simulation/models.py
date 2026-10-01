"""
مدل‌های دادهٔ شبیه‌سازی (فاز ۲).

همه‌چیز در این فایل **دادهٔ خالص** است: dataclass و دیکشنری. هیچ نمونه‌ای از
مدل‌های Django در snapshot یا projected state حمل نمی‌شود، چون شبیه‌سازی باید
کاملاً خالص باشد و نتواند state پایدار را لمس کند.
"""
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

ZERO = Decimal('0.00')
CENT = Decimal('0.01')


def q2(value) -> Decimal:
    """گِرد کردن به دو رقم اعشار — همان قاعدهٔ ``inventory.services.q2``."""
    if value is None:
        return ZERO
    try:
        return Decimal(value).quantize(CENT)
    except (TypeError, ValueError, ArithmeticError):
        return ZERO


def money(value) -> Optional[str]:
    """نمایش رشته‌ای برای خروجی JSON؛ هرگز float برنمی‌گردانیم."""
    if value is None:
        return None
    return str(Decimal(value).quantize(CENT))


# ----------------------------------------------------------------------
# snapshot
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class RawMaterialSnapshot:
    """عکس لحظه‌ای یک مادهٔ اولیه — فقط اعداد، نه مدل Django."""

    pk: int
    name: str
    unit: str
    pack_size: Decimal
    current_stock: Decimal
    min_stock_alert: Decimal

    def with_stock(self, extra) -> 'RawMaterialSnapshot':
        """کپی با موجودی تغییریافته — بدون دست زدن به شیء اصلی."""
        return replace(self, current_stock=q2(Decimal(self.current_stock) + Decimal(extra)))


@dataclass(frozen=True)
class OpenIssueSnapshot:
    """یک درخواست مواد باز، فقط‌خواندنی و بدون وابستگی به ORM."""

    pk: int
    remaining_quantity: Decimal

    def remaining(self) -> Decimal:
        return q2(self.remaining_quantity)


@dataclass(frozen=True)
class MaterialSnapshot:
    """یک ماده به‌همراه باقی‌ماندهٔ سالن و درخواست‌های بازش."""

    raw: RawMaterialSnapshot
    leftover: Decimal = ZERO
    issues: Tuple[OpenIssueSnapshot, ...] = ()

    def total_need(self) -> Decimal:
        return q2(sum((issue.remaining() for issue in self.issues), ZERO))


@dataclass(frozen=True)
class QueueEntry:
    """یک سفارش باز از منظر ترتیب صف."""

    order_id: int
    number: Optional[str]
    priority: int
    created_at: Optional[str]
    open_tasks: int = 0


@dataclass(frozen=True)
class Snapshot:
    """
    ورودی موتور شبیه‌سازی.

    ساخت این شیء تنها جایی است که شبیه‌سازی دیتابیس را *می‌خواند*؛ خودِ
    موت (``engine.run``) هیچ دسترسی به پایگاه‌داده ندارد.
    """

    kind: str
    materials: Tuple[MaterialSnapshot, ...] = ()
    queue: Tuple[QueueEntry, ...] = ()
    captured_at: str = ''

    def material(self, pk) -> Optional[MaterialSnapshot]:
        for item in self.materials:
            if item.raw.pk == int(pk):
                return item
        return None

    def order(self, order_id) -> Optional[QueueEntry]:
        for item in self.queue:
            if item.order_id == int(order_id):
                return item
        return None


# ----------------------------------------------------------------------
# scenario
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    """سناریوی شبیه‌سازی؛ دادهٔ آن فقط دیکشنری/عدد است."""

    kind: str
    params: Dict[str, Any] = field(default_factory=dict)

    def get(self, key, default=None):
        return self.params.get(key, default)


# ----------------------------------------------------------------------
# خروجی
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Projection:
    """حالت پیش‌بینی‌شدهٔ یک ماده — فقط اعداد."""

    raw_material_id: int
    name: str
    unit: str
    needed: Decimal
    from_leftover: Decimal
    from_stock: Decimal
    packs: int
    pack_size: Decimal
    physical_required: Decimal
    leftover_after: Decimal
    projected_stock: Decimal
    enough: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            'raw_material_id': self.raw_material_id,
            'raw_material': self.name,
            'unit': self.unit,
            'needed': money(self.needed),
            'from_leftover': money(self.from_leftover),
            'from_stock': money(self.from_stock),
            'packs': self.packs,
            'pack_size': money(self.pack_size),
            'physical_required': money(self.physical_required),
            'leftover_after': money(self.leftover_after),
            'projected_stock': money(self.projected_stock),
            'enough': self.enough,
        }


@dataclass(frozen=True)
class QueueChange:
    """تغییر جایگاه یک سفارش در صف — تنها اثر واقعی priority در CraftFlow."""

    order_id: int
    number: Optional[str]
    priority_before: int
    priority_after: int
    queue_position_before: int
    queue_position_after: int
    open_tasks: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            'order_id': self.order_id,
            'order_number': self.number,
            'priority_before': self.priority_before,
            'priority_after': self.priority_after,
            'queue_position_before': self.queue_position_before,
            'queue_position_after': self.queue_position_after,
            'open_tasks': self.open_tasks,
        }


@dataclass(frozen=True)
class Diff:
    """اختلاف حالت فعلی و حالت پیش‌بینی‌شده — همیشه قابل خواندن."""

    entries: Tuple[Dict[str, Any], ...] = ()

    def to_list(self) -> List[Dict[str, Any]]:
        return list(self.entries)

    def __bool__(self):
        return bool(self.entries)


@dataclass(frozen=True)
class SimulationResult:
    """خروجی موتور: قطعی، بدون اثر جانبی، قابل تکرار کامل."""

    scenario_kind: str
    status: str
    projected_state: Dict[str, Any]
    diff: Diff
    evidence: Tuple[Dict[str, Any], ...] = ()
    confidence: str = 'low'
    confidence_reasons: Tuple[str, ...] = ()
    limitations: Tuple[str, ...] = ()
    affected_changes: Tuple[Dict[str, Any], ...] = ()
    no_effect_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'scenario_kind': self.scenario_kind,
            'status': self.status,
            'projected_state': self.projected_state,
            'diff': self.diff.to_list(),
            'evidence': list(self.evidence),
            'confidence': self.confidence,
            'confidence_reasons': list(self.confidence_reasons),
            'limitations': list(self.limitations),
            'affected_changes': list(self.affected_changes),
            'no_effect_reason': self.no_effect_reason,
        }


__all__ = [
    'Diff',
    'MaterialSnapshot',
    'OpenIssueSnapshot',
    'Projection',
    'QueueChange',
    'QueueEntry',
    'RawMaterialSnapshot',
    'Scenario',
    'SimulationResult',
    'Snapshot',
    'money',
    'q2',
]