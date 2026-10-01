"""
رجیستری محدودیت‌های داده (فاز ۲).

قانون بنیادی: **نبودِ داده هرگز به صفر تبدیل نمی‌شود.** هر جایی که CraftFlow
دادهٔ کافی برای یک محاسبه ندارد، اینجا صریح ثبت می‌شود و لایهٔ تحلیل به‌جای
عدد، وضعیت ``insufficient_data`` به‌همراه «کد منبع گمشده» برمی‌گرداند.

هیچ‌کدام از این محدودیت‌ها باگ نیستند؛ بازتاب واقعی دیتابیس فعلی CraftFlow هستند.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Sequence, Tuple

# وضعیت استاندارد برای «داده‌ای وجود ندارد» — نه صفر، نه healthy.
STATUS_INSUFFICIENT_DATA = 'insufficient_data'
STATUS_UNKNOWN = 'unknown'


@dataclass(frozen=True)
class Limitation:
    """یک محدودیت داده: چه چیزی موجود نیست و چه چیزی را مسدود می‌کند."""

    code: str
    message: str
    blocks: Tuple[str, ...] = ()
    source: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'code': self.code,
            'message': self.message,
            'blocks': list(self.blocks),
            'source': self.source,
        }


def _limit(code, message, blocks=(), source=''):
    return Limitation(code=code, message=message, blocks=tuple(blocks), source=source)


# ----------------------------------------------------------------------
# رجیستری واحد — تنها منبع حقیقت دربارهٔ قابلیت‌های غیرقابل محاسبه
# ----------------------------------------------------------------------

LIMITS: Dict[str, Limitation] = {
    lim.code: lim
    for lim in (
        _limit(
            'missing_due_date',
            'Order.due_date در دادهٔ فعلیCraftFlow خالی است؛ تاریخ سررسید سفارش '
            'وجود ندارد و تأخیر فقط با قاعدهٔ سنی «created_at + ۳ روز» تخمین زده می‌شود.',
            blocks=('due_date_math', 'delay_severity_from_deadline'),
            source='product.Order.due_date',
        ),
        _limit(
            'missing_material_mapping',
            'product.Material.raw_material برای همهٔ رکوردها NULL است؛ مسیر BOM → '
            'RawMaterial وجود ندارد و نیاز مواد یک سفارش قابل محاسبه نیست.',
            blocks=('order_bom_requirement', 'order_material_shortage'),
            source='product.Material.raw_material',
        ),
        _limit(
            'partial_paint_material_coverage',
            'پوشش PaintingMaterialRequirement کامل نیست؛ بعضی تسک‌های نقاشی هیچ '
            'نیاز مادهٔ ثبت‌شده‌ای ندارند.',
            blocks=('paint_material_requirement'),
            source='product.PaintingMaterialRequirement',
        ),
        _limit(
            'missing_nonpaint_schedule',
            'زمان‌بندی فقط روی تسک‌های نقاشی ثبت شده است؛ تسک‌های غیرنقاشی '
            'scheduled_start/scheduled_end ندارند.',
            blocks=('nonpaint_schedule_queue_depth', 'nonpaint_finish_date'),
            source='product.ProductionTask.scheduled_start',
        ),
        _limit(
            'missing_nonpaint_duration',
            'مدت‌زمان اجرا فقط در PaintingStage.duration_minutes و '
            'custom_duration_minutes وجود دارد؛ برای ایستگاه‌های غیرنقاشی مدت‌زمانی '
            'ذخیره نشده است.',
            blocks=('station_capacity', 'station_throughput', 'station_duration_estimate'),
            source='product.PaintingStage.duration_minutes',
        ),
        _limit(
            'missing_station_capacity',
            'ظرفیت عددی ایستگاه (ساعت کاری، تعداد کارگر مجاز، نرخ تولید) در CraftFlow '
            'ذخیره نشده است؛ ظرفیت‌سنجی عددی برای ایستگاه‌ها ممکن نیست.',
            blocks=('station_capacity_simulation', 'station_utilization_ratio'),
            source='product.ProductionTask',
        ),
        _limit(
            'missing_dependency_graph',
            'هیچ FK یا جدول وابستگی بین تسک‌ها وجود ندارد.ProductionTask.depends_on '
            'تعریف نشده است و ترتیب فقط با قرارداد مشتق‌شدهٔ (order, part, step_order+1) '
            'استنباط می‌شود؛ این یک گراف وابستگی واقعی نیست.',
            blocks=('critical_path', 'production_dependency_graph', 'reschedule_impact'),
            source='product.ProductionTask.step_order',
        ),
        _limit(
            'missing_quality_events',
            'جدول ProductionDefect در دادهٔ فعلی خالی است؛ هیچ رویداد کیفی وجود ندارد '
            'و نبودِ خرابی قابل گزارش به‌عنوان «سالم» نیست.',
            blocks=('quality_status', 'quality_blocking'),
            source='product.ProductionDefect',
        ),
        _limit(
            'missing_holiday_data',
            'جدول Holiday خالی است و تقویم کاری ثبت‌شده‌ای وجود ندارد؛ '
            'is_working_day فقط بر اساس روز هفته محاسبه می‌شود.',
            blocks=('working_calendar_accuracy', 'calendar_aware_scheduling'),
            source='product.Holiday',
        ),
        _limit(
            'unknown_station_codes',
            'بعضی تسک‌ها station_name خارج از STATION_CHOICES دارند (مثل frez، 2set، '
            'cnc,mon، 300x120). این مقادیر عمداً نگاشت یا حذف نمی‌شوند و به‌صورت '
            'سطل unknown_station گزارش می‌شوند.',
            blocks=('station_bucket_totals', 'per_station_accuracy'),
            source='product.ProductionTask.station_name',
        ),
        _limit(
            'missing_task_timestamp',
            'ProductionTask فیلد created_at ندارد؛ «قدمت تسک» قابل اندازه‌گیری نیست و '
            'قدمت به‌صورت مشتق‌شده از قدیمی‌ترین Order مرتبط تخمین زده می‌شود.',
            blocks=('task_age', 'oldest_pending_age_exact'),
            source='product.ProductionTask',
        ),
        _limit(
            'no_finish_time_model',
            'CraftFlow برای تسک finish_time محاسبه نمی‌کند؛ priority فقط ترتیب صف را '
            'تغییر می‌دهد و روی step_order یا زمان اتمام اثری ندارد.',
            blocks=('completion_time_estimate', 'time_saved_estimate'),
            source='product.Order.priority',
        ),
    )
}


def limitation(code: str) -> Limitation:
    """متن فارسی یک محدودیت؛ اگر کد ناشناخته بود، خود کد برگردانده می‌شود."""
    lim = LIMITS.get(code)
    return lim if lim is not None else _limit(code, f'محدودیت داده: {code}')


def limitation_codes(codes: Iterable[str]) -> Tuple[str, ...]:
    """کدها را نرمال و قطعی (بدون تکرار، به ترتیب) می‌کند."""
    seen, out = set(), []
    for code in codes or ():
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return tuple(out)


def limitation_messages(codes: Sequence[str]) -> Tuple[str, ...]:
    return tuple(limitation(code).message for code in codes)


def limitations_payload(codes: Sequence[str]) -> list:
    return [limitation(code).to_dict() for code in limitation_codes(codes)]


@dataclass(frozen=True)
class Unavailable:
    """
    پاسخ استاندارد برای قابلیتی که دادهٔ لازم را ندارد.

    به‌جای ``{"material_shortage": 0}`` هرگز صفر ساخته نمی‌شود؛ به‌جایش
    ``{"status": "insufficient_data", "reason": "missing_material_mapping"}``.
    """

    reason: str
    status: str = STATUS_INSUFFICIENT_DATA
    detail: str = ''
    blocked_by: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {'status': self.status, 'reason': self.reason}
        if self.detail:
            payload['detail'] = self.detail
        if self.blocked_by:
            payload['blocked_capabilities'] = list(self.blocked_by)
        return payload

    def __bool__(self):
        return False


def unavailable(reason: str, *, status: str = STATUS_INSUFFICIENT_DATA,
                detail: str = '', extra_blocks: Sequence[str] = ()) -> Unavailable:
    """
    سازندهٔ reusable برای «قابل محاسبه نیست».

    ``reason`` باید یکی از کدهای ``LIMITS`` باشد تا متن و بلوک‌های مسدودشده
    به‌صورت خودکار و یکدست تولید شوند.
    """
    lim = LIMITS.get(reason)
    if lim is not None:
        blocks = tuple(lim.blocks) + tuple(extra_blocks)
        return Unavailable(reason=reason, status=status, detail=detail or lim.message,
                           blocked_by=limitation_codes(blocks))
    return Unavailable(reason=reason, status=status, detail=detail or str(reason),
                       blocked_by=limitation_codes(extra_blocks))


__all__ = [
    'LIMITS',
    'Limitation',
    'STATUS_INSUFFICIENT_DATA',
    'STATUS_UNKNOWN',
    'Unavailable',
    'limitation',
    'limitation_codes',
    'limitation_messages',
    'limitations_payload',
    'unavailable',
]