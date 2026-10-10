"""
منطق صف تحویل مواد روزانه — تنها مسیر تحویل مواد به تولید.

قاعدهٔ کلی

    نیاز  -> صف روزانه -> تحویل -> بازگشت -> بستن روز

نیاز از سه جا مشتق می‌شود و هر سه یک شکل دارند: «چه کسی، چه روزی، چه
مقداری از چه ماده‌ای».

    * ایستگاه نقاشی    — ProductionTask(station='paint') از برنامهٔ نقاشی
    * سایر ایستگاه‌ها  — ProductionTask(part.material.raw_material)
    * جبران خرابی     — ProductionDefect با مادهٔ جایگزین تعیین‌شده

انباردار چیزی وارد نمی‌کند؛ صف از برنامه ساخته می‌شود و هر واحد کار فقط
یک‌بار شمرده می‌شود. به همین دلیل نه درخواست تکراری ساخته می‌شود و نه
مصرف دوبار از موجودی کم می‌شود.

مقدار تحویل (تنها نقطهٔ گرد کردن در کل ماژول)

    physical = ceil(planned / pack_size) * pack_size     اگر pack_size > 0
    physical = planned                                   در غیر این صورت

همان ``physical`` از موجودی کم و در ``StockMovement(consumption)`` ثبت
می‌شود؛ بنابراین موجودی انبار همیشه برابر «خروج فیزیکی» است و هر مقدار
اضافهٔ بسته یا همان روز یا پایان روز برمی‌گردد.

منبع حقیقت

    موجودی انبار  → ``StockMovement``
    نیاز و مصرف   → ``DailyMaterialQueue``
    ردیابی         → ``DailyMaterialQueueSource``

هیچ‌کدام از این‌ها تعریف دومی از همدیگر نمی‌سازند؛ گزارش‌ها همیشه از همین
فیلدهای ذخیره‌شده می‌خوانند.
"""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import contextlib
import logging
import threading

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    DailyMaterialQueue,
    RawMaterial,
    StockMovement,
)

_logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')
CENT = Decimal('0.01')


class HandoverError(Exception):
    """خطایی که پیامش مستقیماً به کاربر انبار نمایش داده می‌شود."""


# ---------------------------------------------------------------------------
# کمکی‌ها
# ---------------------------------------------------------------------------

def _q2(value):
    """Round to 2 decimal places. تنها نقطهٔ گرد کردن هنگام نوشتن."""
    if value is None:
        return Decimal('0.00')
    return Decimal(str(value)).quantize(Decimal('0.01'))


def q2(value):
    return Decimal(value).quantize(CENT)


def _dec(value, default='0'):
    """تبدیل امن به Decimal؛ ورودی نامعتبر به مقدار پیش‌فرض برمی‌گردد."""
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return Decimal(default)


# ---------------------------------------------------------------------------
# قانون بسته‌بندی
# ---------------------------------------------------------------------------

def _packs_counted(need, pack_size):
    """
    تعداد بستهٔ کامل لازم برای پوشش *need* — تنها قانون سقف بسته‌بندی در این ماژول.

    ``pack_size <= 0`` یعنی «بسته‌بندی ثابت ندارد» و مقدار دقیق تحویل می‌شود.
    """
    need = _q2(need)
    pack = _q2(pack_size or 0)
    if pack <= 0:
        return 0
    if need <= 0:
        return 0
    return int((need / pack).to_integral_value(rounding=ROUND_CEILING))


def _physical_for(need, pack_size):
    """مقدار فیزیکی تحویل‌شده با گرد کردن رو به بالا به بستهٔ کامل.

    همیشه به بسته کامل بعدی گرد می‌شود (سقف). منطق «بستهٔ باز» بر اساس
    موجودی حذف شد، چون موجودی مضرب بسته نیست لزوماً یعنی بسته باز هست.
    """
    need = _q2(need)
    pack = _q2(pack_size or 0)
    if pack <= 0:
        return 0, need

    count = _packs_counted(need, pack)
    return count, _q2(pack * count)


def _packs_for(need, pack_size):
    """Return number of full packages needed to cover *need*."""
    return _physical_for(need, pack_size)


def _suggested_delivery(need, pack_size, stock):
    """
    مقدار پیشنهادی تحویل: اول باقی‌ماندهٔ بستهٔ باز تمام شود.

    open_remainder = stock mod pack_size
      * اگر بستهٔ باز داریم (remainder > 0): همان باقی‌مانده پیشنهاد می‌شود.
      * اگر بستهٔ باز نداریم: دقیقاً مقدار نیاز.
    بدون اندازهٔ بستهٔ ثابت: مقدار نیاز.
    """
    need = _q2(need)
    pack = _q2(pack_size or 0)
    stock_val = _q2(stock or 0)

    if pack <= 0:
        return need

    open_remainder = _q2(stock_val % pack)
    if open_remainder > 0:
        return open_remainder

    return need


def receive_quantity(raw_material, pack_count):
    """
    مقدار انبار از تعداد بسته — قانون ورود کالا.

    همان ``pack_size`` است که هنگام تحویل به کارگر گرد می‌شود، پس عددی که
    اسکن وارد می‌کند دقیقاً همان چیزی است که بعداً از انبار کم می‌شود.
    """
    count = _dec(pack_count)
    if not count.is_finite() or count < 1:
        raise HandoverError('تعداد بسته باید عددی بزرگ‌تر از صفر باشد.')
    pack = _q2(raw_material.pack_size or 0)
    if pack <= 0:
        raise HandoverError(
            f'برای «{raw_material.name}» اندازهٔ بسته تعریف نشده است. '
            'اول در صفحهٔ «مواد اولیه» اندازهٔ بسته را وارد کنید.',
        )
    return _q2(pack * count)


# ---------------------------------------------------------------------------
# تاریخ کاری
# ---------------------------------------------------------------------------

def _work_date(value):
    """Convert a (possibly tz-aware) datetime/date into the local work date."""
    if value is None:
        return None
    if hasattr(value, 'time'):
        try:
            return timezone.localtime(value).date()
        except Exception:
            return value.date()
    return value


def _task_date(task):
    return _work_date(task.scheduled_start)


def _day_bounds(date):
    """بازهٔ [start, end) برای یک تاریخ کاری، با آگاهی از منطقهٔ زمانی."""
    from datetime import datetime, timedelta

    day_start = datetime.combine(date, datetime.min.time())
    start = day_start if timezone.is_aware(day_start) else timezone.make_aware(day_start)
    return start, start + timedelta(days=1)


def _is_working(date):
    import jdatetime
    from product.utils import is_working_day

    return is_working_day(jdatetime.date.fromgregorian(date=date))


def _shift_working_date(date, step, limit=14):
    """نزدیک‌ترین روز کاری قبل (step=-1) یا بعد (step=+1)؛ جمعه و تعطیلات رد می‌شوند."""
    from datetime import timedelta

    current = date
    for _ in range(limit):
        current = current + timedelta(days=step)
        if _is_working(current):
            return current
    return None


def _delivery_status(queue):
    """وضعیت ردیف از روی مقادیر؛ تنها تعریف این قاعده."""
    delivered = _q2(queue.delivered_quantity)
    returned = _q2(queue.returned_quantity)
    if delivered <= 0:
        return 'pending'
    if returned >= delivered:
        return 'returned'
    if delivered < _q2(queue.planned_quantity):
        return 'partial'
    return 'delivered'


# ---------------------------------------------------------------------------
# نیاز ایستگاه نقاشی
# ---------------------------------------------------------------------------

def _resolve_painting_requirements_for_task(task):
    """
    تنها مسیر صف روزانه به نیاز نقاشی.

    از همان resolver canonical در ``product.utils`` استفاده می‌کند؛ هر عضوش
    dict با کلیدهای ``requirement`` و ``raw_material`` برمی‌گردد.
    """
    from product.utils import get_resolved_painting_requirements_for_task
    return get_resolved_painting_requirements_for_task(task)


def _painting_work_unit_key(task, raw_material_id):
    """
    کلید «واحد کار نقاشی» — مبنای de-duplication نیاز مواد.

    ``PaintingMaterialRequirement`` در سطح (process + stage + product + color_part)
    تعریف شده. یک ``OrderItem`` برای هر مرحله یک ``ProductionTask`` جداگانه دارد،
    بنابراین اگر فرمول برای هر task اعمال شود به تعداد مراحل ضرب می‌شود: ۶ مرحله × ۱٫۳ = ۷٫۸ به‌جای ۲٫۶.

    واحد کار نقاشی = (order_item, color_part, painting_process, stage, raw_material).

    de-duplication داخل هر گروه (worker, raw_material) انجام می‌شود؛ بنابراین
    دو کارگر مستقل که در یک روز روی دو واحد کار متفاوت کار می‌کنند هر کدام
    نیاز خودشان را نگه می‌دارند، و همان واحد کار در روز دیگر دوباره شمرده
    می‌شود چون صف هر روز مستقل است.
    """
    return (
        task.order_item_id,
        task.color_part or '',
        task.painting_stage.process_id,
        task.painting_stage_id,
        raw_material_id,
    )


def task_material_requirements(task):
    """
    نیاز مادهٔ اولیهٔ یک تسک از فرمول ساخت (BOM) — فقط خواندنی.

    خروجی: لیست ``(raw_material, quantity)``. برای ایستگاه نقاشی از فرمول
    ``PaintingMaterialRequirement`` می‌آید و برای سایر ایستگاه‌ها از مادهٔ
    قطعه. این همان محاسبه‌ای است که صف روزانه از آن مشتق می‌شود، پس گزارش‌ها و
    ابزارهای بیرونی نباید نسخهٔ دوم خودشان را بنویسند.
    """
    if task.station_name == 'paint':
        resolved, _errors, _context = _resolve_painting_requirements_for_task(task)
        task_qty = _dec(task.quantity)
        rows = []
        for entry in resolved:
            raw = entry.get('raw_material')
            if not raw:
                continue
            consumption = _dec(entry['requirement'].consumption_per_unit)
            quantity = task_qty * consumption
            if quantity > 0:
                rows.append((raw, quantity))
        return rows
    return _station_requirements(task)


# ---------------------------------------------------------------------------
# نیاز سایر ایستگاه‌ها (برش، مونتاژ، …)
# ---------------------------------------------------------------------------

def _station_requirements(task):
    """
    نیاز مادهٔ اولیهٔ یک تسک غیرنقاشی، از مادهٔ قطعهٔ آن.

    خروجی: لیستی از ``(raw_material, quantity)``. برای ایستگاه نقاشی همیشه
    خالی است — نیاز نقاشی از ``_resolve_painting_requirements_for_task``
    می‌آید، چون فرمولش در ``PaintingMaterialRequirement`` تعریف شده نه در قطعه.
    """
    if task.station_name == 'paint':
        return []

    part = task.part
    if not part or not getattr(part, 'material_id', None):
        return []

    material = part.material
    if not material or not material.raw_material_id:
        return []

    raw = material.raw_material
    per_unit = _dec(material.consumption_per_unit, default='0')
    if per_unit <= 0:
        return []

    quantity = _dec(task.quantity) * per_unit
    if quantity <= 0:
        return []

    return [(raw, quantity)]


# ---------------------------------------------------------------------------
# واجد شرایط بودن تسک برای صف
# ---------------------------------------------------------------------------

_SKIP_REASONS = {
    'no_schedule': 'بدون تاریخ زمان‌بندی',
    'no_worker': 'بدون کارگر تخصیص‌یافته',
    'no_stage': 'بدون مرحله نقاشی',
    'no_order_item': 'بدون آیتم سفارش',
    'no_color_part': 'بدون بخش رنگی',
    'no_quantity': 'بدون مقدار تولید',
    'no_requirement': 'بدون فرمول مصرف',
    'unresolved_material': 'نیاز به ماده اولیه واقعی نگاشت نشد',
    'no_part': 'بدون قطعهٔ متصل به ماده اولیه',
    'done': 'تسک تمام‌شده',
}


def task_queue_ineligibility(task):
    """Return None if the task may feed the daily queue, else a skip reason key.

    قاعدهٔ مشترک هر دو نوع نیاز: تاریخ، کارگر و مقدار باید مشخص باشند.
    تسکی که کارگر ندارد هرگز به صف تحویل انبار نمی‌آید.
    """
    if _task_date(task) is None:
        return 'no_schedule'
    if not task.assigned_worker_id:
        return 'no_worker'
    if not task.quantity or int(task.quantity) <= 0:
        return 'no_quantity'

    if task.station_name == 'paint':
        if not task.painting_stage_id:
            return 'no_stage'
        if not task.order_item_id or not task.order_item.product_id:
            return 'no_order_item'
        if not task.color_part:
            return 'no_color_part'
        return None

    # سایر ایستگاه‌ها: نیاز از قطعه/ماده می‌آید.
    part = task.part
    if not part or not getattr(part, 'material_id', None):
        return 'no_requirement'
    if not part.material.raw_material_id:
        return 'no_requirement'
    return None


# ---------------------------------------------------------------------------
# تجمیع نیاز یک روز
# ---------------------------------------------------------------------------

def aggregate_queue_requirements(tasks, date=None):
    """
    تجمیع نیاز مواد *tasks* یک روز به تفکیک (کارگر، ماده).

    Returns ``(grouped, diagnostics)``:
        grouped:      OrderedDict با کلید ``(worker_id, raw_material_id)``؛
                     هر مقدار ``{'worker', 'raw_material', 'quantity', 'sources'}``
                     که ``sources`` فهرستی از
                     ``(kind, task, stage, defect, raw, qty)`` است.
        diagnostics:  ``{'skipped': [...], 'multistage_overlaps': [...],
                        'unresolved_materials': [...], 'mapping_notes': [...]}``

    ``date`` تاریخ کاری همان صف است و فقط برای فیلتر کردن نیازِ جبران خرابی
    لازم است: هر خرابی به روزِ خودش تعلق دارد. اگر ``None`` داده شود، همهٔ
    خرابی‌های فعال بدون فیلتر تاریخ وارد می‌شوند که فقط برای تست‌های واحد
    قابل قبول است، نه برای ساخت صف روزانه.

    قاعده‌ها:
        * مقدار نیاز از ``ProductionTask.quantity`` می‌آید نه از
          ``completed_quantity``، تا تسک نیمه‌کاره نیاز کاملش را نگه دارد؛
        * کارگر همیشه همان کارگر تخصیص‌یافته است؛
        * نیاز نقاشی یک‌بار به‌ازای هر «واحد کار نقاشی» شمرده می‌شود، نه یک‌بار
          به‌ازای هر ``PaintingStage``. اگر چند تسک یک واحد کار باشند، بزرگ‌ترین
          مقدار تولید اندازهٔ واحد را تعیین می‌کند و بقیه به‌عنوان منبعِ بدون سهم
          مستقل نگه داشته می‌شوند تا ``sum(source.quantity) == quantity`` بماند؛
        * تسک‌های تسکل��‌شده (``status='done'``) نیاز تازه‌ای ندارند و رد می‌شوند،
          ولی اگر تحویلی از قبل ثبت شده باشد، ردیف صف آن را حفظ می‌کند.
    """
    grouped = OrderedDict()
    diagnostics = {
        'skipped': [], 'multistage_overlaps': [],
        'unresolved_materials': [], 'mapping_notes': [],
    }

    for task in tasks:
        if task.status == 'done':
            continue

        reason = task_queue_ineligibility(task)
        if reason is not None:
            diagnostics['skipped'].append(
                {'task_id': task.pk, 'reason': reason,
                 'label': _SKIP_REASONS.get(reason, reason)}
            )
            continue

        if task.station_name == 'paint':
            _accumulate_painting_task(task, grouped, diagnostics)
        else:
            _accumulate_station_task(task, grouped, diagnostics)

    _accumulate_rework_needs(grouped, diagnostics, date)
    _accumulate_carryover(grouped, diagnostics, date)

    # نهایی‌سازی پیش از سنجش هم‌پوشانی لازم است: واحدهای کار نقاشی تا وقتی به
    # سهم قطعی تبدیل نشده باشند در ``stages_by_process`` ثبت نمی‌شوند و تشخیص
    # هم‌پوشانی همیشه خالی می‌ماند.
    _finalise_grouped(grouped)

    for (worker_id, raw_id, stage_id, color_part), entry in grouped.items():
        stages = entry.get('stages_by_process')
        if not stages:
            continue
        for process_id, stage_ids in stages.items():
            if len(stage_ids) > 1:
                diagnostics['multistage_overlaps'].append({
                    'worker_id': worker_id,
                    'raw_material_id': raw_id,
                    'process_id': process_id,
                    'stage_ids': sorted(stage_ids.keys()),
                    'total_quantity': _q2(
                        sum(stage_ids.values())),
                })

    return grouped, diagnostics


def _entry_for(grouped, worker, raw, stage=None, color_part=''):
    """گروه تجمع برای (کارگر، ماده، مرحله، بخش رنگی)، یا ساختن یک گروه تازه."""
    stage_id = stage.pk if stage else None
    key = (worker.pk, raw.pk, stage_id, color_part)
    entry = grouped.get(key)
    if entry is None:
        entry = grouped[key] = {
            'worker': worker,
            'raw_material': raw,
            'painting_stage': stage,
            'color_part': color_part,
            'quantity': ZERO,
            'sources': [],
            'stages_by_process': {},
            'work_units': {},
        }
    return entry


def _accumulate_painting_task(task, grouped, diagnostics):
    resolved, resolution_errors, context = _resolve_painting_requirements_for_task(task)

    if resolution_errors:
        # نیاز وجود دارد ولی مادهٔ واقعی‌اش قابل تعیین نیست؛ هیچ صفی با مادهٔ
        # حدسی ساخته نمی‌شود و علت دقیق گزارش می‌شود.
        diagnostics['unresolved_materials'].extend(
            error.as_dict() for error in resolution_errors
        )

    if not resolved:
        if resolution_errors:
            diagnostics['skipped'].append(
                {'task_id': task.pk, 'reason': 'unresolved_material',
                 'label': _SKIP_REASONS['unresolved_material'],
                 'detail': resolution_errors[0].as_dict()}
            )
        else:
            diagnostics['skipped'].append(
                {'task_id': task.pk, 'reason': 'no_requirement',
                 'label': _SKIP_REASONS['no_requirement'],
                 'detail': {
                     'process_id': context.get('process_id'),
                     'product_id': context.get('product_id'),
                     'color_part': context.get('color_part'),
                     'color_code': context.get('color_code'),
                 }}
            )
        return

    worker = task.assigned_worker
    stage = task.painting_stage
    task_qty = _dec(task.quantity)

    for resolved_entry in resolved:
        raw = resolved_entry['raw_material']
        note = resolved_entry.get('note')
        if note is not None:
            diagnostics['mapping_notes'].append(note.as_dict())

        consumption = _dec(resolved_entry['requirement'].consumption_per_unit)
        # جمع با دقت کامل انجام می‌شود و فقط یک‌بار هنگام نوشتن گرد می‌شود؛
        # گرد کردن هر منبع جداگانه نیازهای کوچک را بی‌صدا حذف می‌کرد.
        qty = task_qty * consumption
        if qty <= 0:
            continue

        entry = _entry_for(grouped, worker, raw, stage, task.color_part or '')

        unit_key = _painting_work_unit_key(task, raw.pk)
        unit = entry['work_units'].get(unit_key)
        if unit is None:
            entry['work_units'][unit_key] = {
                'raw_material': raw,
                'process_id': stage.process_id,
                'quantity': qty,
                'representative': (task, stage, raw, qty),
                'shared': [],
            }
        elif qty > unit['quantity']:
            # بزرگ‌ترین مقدار تولید اندازهٔ واحد کار را تعیین می‌کند؛ سهم قبلی
            # به صفر می‌رسد تا جمع منابع با مقدار صف برابر بماند.
            unit['shared'].append(unit['representative'][:2])
            unit['representative'] = (task, stage, raw, qty)
            unit['quantity'] = qty
        else:
            unit['shared'].append((task, stage))


def _accumulate_station_task(task, grouped, diagnostics):
    rows = _station_requirements(task)
    if not rows:
        diagnostics['skipped'].append(
            {'task_id': task.pk, 'reason': 'no_requirement',
             'label': _SKIP_REASONS['no_requirement']}
        )
        return

    worker = task.assigned_worker
    for raw, qty in rows:
        if qty <= 0:
            continue
        entry = _entry_for(grouped, worker, raw, None, '')
        entry['quantity'] += qty
        entry['sources'].append(('station', task, None, None, raw, qty))


def _accumulate_rework_needs(grouped, diagnostics, date=None):
    """
    نیاز جبران خرابی را به همان گروه‌ها اضافه می‌کند.

    یک خرابی وقتی نیاز دارد که مادهٔ جایگزین، مقدار، کارگر و تاریخ کاری‌اش
    مشخص باشد. تاریخ «روزِ درخواست جبران» است، نه تاریخ تسک خراب‌شده؛ چون
    خرابی معمولاً بعد از آن تاریخ کشف می‌شود و نیاز باید همان روز قابل تحویل
    باشد.

    ``date`` تاریخ کاری صف در حال ساخت است و فیلتر اجباری این مسیر است؛ بدون
    آن، یک خرابیِ فعال به صف *همهٔ* روزها اضافه می‌شد و هر روز چندبار تحویل
    می‌شد.
    """
    from product.models import ProductionDefect

    defects = (
        ProductionDefect.objects
        .filter(
            material_raw_material__isnull=False,
            material_quantity__isnull=False,
            material_worker__isnull=False,
            material_work_date__isnull=False,
            status__in=('reported', 'material_requested'),
        )
        .exclude(material_quantity__lte=0)
        .select_related('material_raw_material', 'material_worker', 'order')
        .order_by('id')
    )
    if date is not None:
        defects = defects.filter(material_work_date=date)
    else:
        _logger.warning(
            '_accumulate_rework_needs called without a work date; every active '
            'rework defect will be added to this queue.'
        )

    for defect in defects:
        raw = defect.material_raw_material
        worker = defect.material_worker
        qty = _q2(defect.material_quantity)
        entry = _entry_for(grouped, worker, raw, None, '')
        entry['quantity'] += qty
        entry['sources'].append(('rework', None, None, defect, raw, qty))


def _accumulate_carryover(grouped, diagnostics, date):
    """
    کسری تحویل روز کاری قبل (ردیف partial) را به همان کارگر و ماده در *date* می‌آورد.
    در tuple منبع، جایگاه `task` برای نوع carryover ردیف مبدأ را نگه می‌دارد.
    """
    if date is None:
        return
    previous = _shift_working_date(date, -1)
    if previous is None:
        return
    rows = (
        DailyMaterialQueue.objects
        .filter(work_date=previous, status='partial', delivered_quantity__gt=0)
        .select_related('worker', 'raw_material')
    )
    for row in rows:
        shortfall = _q2(row.planned_quantity) - _q2(row.delivered_quantity)
        if shortfall <= 0:
            continue
        entry = _entry_for(grouped, row.worker, row.raw_material, None, '')
        entry['quantity'] += shortfall
        entry['sources'].append(('carryover', row, None, None, row.raw_material, shortfall))


def _finalise_grouped(grouped):
    """
    جمع نهایی از واحدهای کار یکتا ساخته می‌شود، نه از تعداد taskها.

    در این مرحله ``work_units`` به سهم قطعی تبدیل و به ``sources`` منتقل
    می‌شود تا ``sum(source.quantity) == planned_quantity`` تضمین شود.
    """
    for entry in grouped.values():
        for unit in entry.pop('work_units', {}).values():
            task, stage, raw, qty = unit['representative']
            entry['quantity'] += unit['quantity']
            entry['sources'].append(('painting', task, stage, None, raw, qty))
            for shared_task, shared_stage in unit['shared']:
                # ردیف منبع بدون سهم مستقل: فقط نشان می‌دهد این مرحله در همان
                # واحد کار نقاشی شریک است (traceability بدون افزودن به جمع).
                entry['sources'].append(
                    ('painting', shared_task, shared_stage, None, raw, ZERO))
            stages = entry['stages_by_process'].setdefault(unit['process_id'], {})
            stages[stage.id] = stages.get(stage.id, ZERO) + unit['quantity']


# ---------------------------------------------------------------------------
# گزارش‌های تشخیصی مشتق از تجمیع
# ---------------------------------------------------------------------------

def detect_multistage_overlaps(date):
    """
    Report-only: واحد کار نقاشی یک process که از راه مراحل مختلف به همان
    کارگر/ماده در *date* می‌رسد. بدون نوشتن در دیتابیس.

    چون نیاز یک‌بار به‌ازای هر واحد کار شمرده می‌شود، این یک مشاهدهٔ
    برنامه‌ریزی است، نه بادکردن مقدار.
    """
    tasks = _get_scheduled_painting_tasks(date)
    grouped, diagnostics = aggregate_queue_requirements(tasks, date)
    
    # Cross-entry multistage overlap detection:
    # Check if the same worker/order_item/color_part/process/raw_material
    # has tasks for multiple stages (across different queue entries).
    from collections import defaultdict
    work_unit_stages = defaultdict(set)
    for entry in grouped.values():
        for source in entry['sources']:
            if source[0] == 'painting' and source[1]:  # source[1] is task
                task = source[1]
                if task.order_item_id and task.color_part and task.painting_stage:
                    key = (
                        entry['worker'].pk,
                        entry['raw_material'].pk,
                        task.order_item_id,
                        task.color_part,
                        task.painting_stage.process_id,
                    )
                    work_unit_stages[key].add(task.painting_stage_id)
    
    overlaps = []
    for key, stage_ids in work_unit_stages.items():
        if len(stage_ids) > 1:
            worker_id, raw_id, order_item_id, color_part, process_id = key
            overlaps.append({
                'worker_id': worker_id,
                'raw_material_id': raw_id,
                'order_item_id': order_item_id,
                'color_part': color_part,
                'process_id': process_id,
                'stage_ids': sorted(stage_ids),
            })
    
    return overlaps


def log_queue_diagnostics(date, diagnostics):
    """Surface aggregation diagnostics (skipped tasks, stage overlaps)."""
    for item in diagnostics.get('multistage_overlaps') or []:
        _logger.warning(
            'sync_daily_material_queue: %s — process %s reaches the same '
            'worker/material through stages %s (total %s). Each painting work '
            'unit is counted once; these are separate work units.',
            date, item['process_id'], item['stage_ids'], item['total_quantity'],
        )
    seen_notes = set()
    for item in diagnostics.get('mapping_notes') or []:
        note_key = (item.get('requirement_id'), item.get('process_id'),
                    item.get('slot_raw_material_id'))
        if note_key in seen_notes:
            continue
        seen_notes.add(note_key)
        # نقص دادهٔ کاتالوگ: نیاز به مادهٔ FK خودش حل شد، پس این فقط گزارش است.
        _logger.info(
            'sync_daily_material_queue: %s — نیاز نقاشی #%s (process=%s, '
            'material=%s): %s',
            date, item.get('requirement_id'), item.get('process_id'),
            item.get('slot_raw_material_id'), item.get('label'),
        )
    seen_unresolved = set()
    for item in diagnostics.get('unresolved_materials') or []:
        key = (item.get('requirement_id'), item.get('process_id'),
               item.get('slot_raw_material_id'), item.get('color_code'))
        if key in seen_unresolved:
            continue
        seen_unresolved.add(key)
        # نیاز نقاشی به ماده اولیه واقعی نگاشت نشد؛ عمداً هیچ صفی با مادهٔ
        # حدسی ساخته نمی‌شود، پس این ردیف‌ها باید توسط انسان اصلاح شوند.
        _logger.error(
            'sync_daily_material_queue: %s — نیاز نقاشی #%s (process=%s, '
            'slot_raw_material=%s, color_code=%s) به ماده اولیه واقعی نگاشت '
            'نشد: %s',
            date, item.get('requirement_id'), item.get('process_id'),
            item.get('slot_raw_material_id'), item.get('color_code'),
            item.get('label'),
        )
    skipped = diagnostics.get('skipped') or []
    if skipped:
        _logger.info(
            'sync_daily_material_queue: %s — %s task(s) did not enter the queue.',
            date, len(skipped),
        )
    return diagnostics.get('multistage_overlaps') or []


# ---------------------------------------------------------------------------
# خواندن تسک‌های یک روز
# ---------------------------------------------------------------------------

_TASK_RELATED = (
    'painting_stage', 'order_item', 'order_item__product',
    'assigned_worker', 'scanned_by', 'part', 'part__material',
)


def _get_scheduled_tasks(date, station_name=None):
    from product.models import ProductionTask

    start, end = _day_bounds(date)
    qs = ProductionTask.objects.filter(
        scheduled_start__gte=start, scheduled_start__lt=end,
    ).select_related(*_TASK_RELATED)
    if station_name:
        qs = qs.filter(station_name=station_name)
    return list(qs)


def _get_scheduled_painting_tasks(date):
    return _get_scheduled_tasks(date, station_name='paint')


# ---------------------------------------------------------------------------
# ساخت و همگام‌سازی صف
# ---------------------------------------------------------------------------

CONFLICT_NOTE_PLAN_CHANGED = (
    'تغییر برنامه پس از ثبت تحویل/بازگشت اعمال نشد؛ تاریخچهٔ واقعی حفظ شد.'
)
CONFLICT_NOTE_DROPPED = (
    'نیاز از برنامه حذف شد ولی تحویل/بازگشت ثبت‌شده دارد؛ '
    'موجودی و تاریخچه دست‌نخورده ماند.'
)


def _mark_conflict(queue, note):
    """Flag a plan/transaction conflict without touching real movement history."""
    queue.has_plan_conflict = True
    queue.conflict_note = (note or '')[:255]
    queue.save(update_fields=['has_plan_conflict', 'conflict_note', 'updated_at'])


def _clear_conflict_if_resolved(queue):
    """
    Clear conflict flag if the plan now matches actual consumption.

    A conflict is considered resolved when:
    - The queue has transactions (delivered/returned)
    - The actual consumption equals the planned quantity (within rounding)
    """
    if not queue.has_plan_conflict:
        return False

    if not queue.has_transaction:
        return False

    actual = _q2(queue.actual_consumption)
    planned = _q2(queue.planned_quantity)
    if actual == planned:
        queue.has_plan_conflict = False
        queue.conflict_note = ''
        queue.save(update_fields=['has_plan_conflict', 'conflict_note', 'updated_at'])
        return True

    return False


def build_daily_queue_for_date(date):
    """
    ساخت (یا به‌روزرسانی) ردیف‌های ``DailyMaterialQueue`` برای *date*.

    نیاز روز از برنامهٔ تولید مشتق می‌شود: تسک‌های آن روز (نقاشی و سایر
    ایستگاه‌ها) به‌علاوهٔ نیازهای جبران خرابی، گروه‌بندی‌شده به تفکیک
    (کارگر، ماده) با ردیابی کامل در ``DailyMaterialQueueSource``.

    محافظ تراکنشی:
        * ردیف بدون تحویل/بازگشت کاملاً همگام می‌شود;
        * ردیفی که ``delivered_quantity > 0`` یا ``returned_quantity > 0``
          دارد، نیاز و منابعش دست‌نخورده می‌ماند و فقط تغییر برنامه با
          ``has_plan_conflict`` / ``conflict_note`` علامت می‌خورد. حرکات
          واقعی انبار هرگز بازنویسی نمی‌شوند.

    Idempotent: اجرای مکرر هیچ ردیف یا منبعی را تکرار نمی‌کند.

    Returns: (queue_count, diagnostics)
    """
    from .models import DailyMaterialQueueSource

    tasks = _get_scheduled_tasks(date)
    grouped, diagnostics = aggregate_queue_requirements(tasks, date)

    with transaction.atomic():
        existing = list(
            DailyMaterialQueue.objects.select_for_update()
            .filter(work_date=date)
        )
        existing_keys = {(q.worker_id, q.raw_material_id, q.painting_stage_id, q.color_part): q for q in existing}

        for (worker_id, raw_id, stage_id, color_part), entry in grouped.items():
            # تنها نقطهٔ گرد کردن: مقدار نهایی و قابل ذخیره در فیلد دومقطری.
            # حتی اگر planned به 0.00 گرد شود، ردیف باید ساخته شود تا sources
            # برای ردیابی ثبت بمانند.
            planned = _q2(entry['quantity'])
            queue = existing_keys.get((worker_id, raw_id, stage_id, color_part))

            if queue is None:
                if planned <= 0 and not entry['sources']:
                    continue
                queue = DailyMaterialQueue(
                    work_date=date,
                    worker=entry['worker'],
                    raw_material=entry['raw_material'],
                    painting_stage=entry.get('painting_stage'),
                    color_part=entry.get('color_part', ''),
                    planned_quantity=planned,
                    status='pending',
                )
                queue.save()
            elif queue.has_transaction:
                # تاریخچهٔ تحویل/بازگشت از نیازِ برنامه‌ریزی‌شده مهم‌تر است.
                if _q2(queue.planned_quantity) != planned:
                    _mark_conflict(queue, CONFLICT_NOTE_PLAN_CHANGED)
                continue
            else:
                queue.planned_quantity = planned
                queue.worker = entry['worker']
                queue.raw_material = entry['raw_material']
                queue.painting_stage = entry.get('painting_stage')
                queue.color_part = entry.get('color_part', '')
                if queue.status == 'cancelled':
                    # نیاز دوباره به برنامه برگشته است: ردیف را زنده کن.
                    queue.status = 'pending'
                queue.has_plan_conflict = False
                queue.conflict_note = ''
                queue.save()

            # منابع فقط برای ردیف‌های بدون حرکت واقعی تازه می‌شوند.
            queue.sources.all().delete()
            for kind, task, stage, defect, raw, qty in entry['sources']:
                origin = task if kind == 'carryover' else None
                DailyMaterialQueueSource.objects.create(
                    queue=queue, kind=kind,
                    production_task=None if origin else task,
                    carryover_from=origin,
                    painting_stage=stage, defect=defect,
                    raw_material=raw, quantity=_q2(qty),
                )

        # ردیف‌هایی که دیگر جزو برنامه نیستند.
        for key, queue in existing_keys.items():
            if key in grouped:
                continue
            if queue.has_transaction:
                if not queue.has_plan_conflict:
                    _mark_conflict(queue, CONFLICT_NOTE_DROPPED)
                continue
            # بدون تراکنش واقعی، منابع هم باید پاک شوند؛ وگرنه تسکی که
            # جابه‌جا شده هم در ردیف لغوشده و هم در ردیف جدید می‌ماند و در
            # ردیابی مواد سفارش دوبار شمرده می‌شود.
            queue.sources.all().delete()
            if queue.status != 'cancelled':
                queue.status = 'cancelled'
                queue.save(update_fields=['status', 'updated_at'])

    log_queue_diagnostics(date, diagnostics)

    return len(grouped), diagnostics

    return list(
        DailyMaterialQueue.objects
        .filter(work_date=date)
        .order_by('worker', 'raw_material__name')
    )


def sync_daily_material_queue(date):
    """
    نقطهٔ مرکزی همگام‌سازی. صف *date* را از برنامهٔ تولید بازمی‌سازد.
    """
    from django.db import transaction as db_transaction

    with db_transaction.atomic():
        return build_daily_queue_for_date(date)


def sync_daily_material_queue_for_dates(dates):
    """
    چند تاریخ کاری را در **یک** تراکنش همگام می‌کند.

    جابه‌جایی یک تسک از روز ۱ به روز ۲ باید نیاز روز ۱ را حذف و نیاز روز ۲
    را بسازد؛ اگر این دو تراکنش جدا باشند، لحظه‌ای وجود دارد که روز ۱ نیازِ
    کاری را نشان می‌دهد که دیگر آنجا نیست.
    """
    from django.db import transaction as db_transaction

    results = {}
    all_diagnostics = {}
    with db_transaction.atomic():
        for value in dates:
            queue_count, diagnostics = build_daily_queue_for_date(value)
            results[value] = queue_count
            all_diagnostics[value] = diagnostics
    return results, all_diagnostics


def _affected_dates_for_tasks(task_ids):
    """تاریخ‌های کاری تحت تأثیر تسک‌های داده‌شده."""
    if not task_ids:
        return set()
    from product.models import ProductionTask

    dates = set()
    rows = (
        ProductionTask.objects
        .filter(pk__in=list(task_ids))
        .values_list('scheduled_start', flat=True)
    )
    for value in rows:
        work_date = _work_date(value)
        if work_date is not None:
            dates.add(work_date)
    return dates


_queue_sync_state = threading.local()


def _pending_sync_dates():
    return getattr(_queue_sync_state, 'dates', None)


@contextlib.contextmanager
def queue_sync_scope():
    """
    جمع‌آوری تاریخ‌های sync درون یک بلوک و flush یک‌باره در پایان.

    مجموعهٔ در حال رشد به فراخوان داده می‌شود تا بتواند ببیند چه روزهایی تا این
    لحظه صف نیاز به بازسازی دارند.
    """
    previous = getattr(_queue_sync_state, 'dates', None)
    pending = set()
    _queue_sync_state.dates = pending
    try:
        yield pending
        if pending:
            _flush_sync_dates(sorted(pending))
    finally:
        _queue_sync_state.dates = previous


def _flush_sync_dates(dates):
    from django.db import transaction as db_transaction

    try:
        sync_daily_material_queue_for_dates(dates)
    except Exception:
        _logger.exception('queue_sync_scope: failed to sync dates %s', dates)


def request_queue_sync(dates):
    """
    درخواست بازسازی صف برای تاریخ‌های داده‌شده. Best effort: هرگز raise نمی‌کند
    و منطق اصلی فراخوان را نمی‌شکند. درون یک ``queue_sync_scope`` فقط تاریخ
    را جمع می‌کند و flush را به پایان بلوک واگذار می‌کند.
    """
    values = [d for d in (dates or ()) if d is not None]
    if not values:
        return []

    pending = _pending_sync_dates()
    if pending is not None:
        pending.update(values)
        return []

    try:
        return sync_daily_material_queue_for_dates(values)
    except Exception:
        _logger.exception('request_queue_sync: failed to sync %s', values)
        raise


def request_queue_sync_for_tasks(task_ids, extra_dates=()):
    """درخواست sync برای تاریخ‌های *task_ids* به‌علاوهٔ *extra_dates*."""
    dates = set(_affected_dates_for_tasks(task_ids))
    dates |= {_work_date(d) for d in (extra_dates or ()) if d is not None}
    return request_queue_sync(dates)


def sync_queue_for_tasks(task_ids, *, extra_dates=()):
    """بازسازی صف برای همهٔ تاریخ‌های تحت تأثیر *task_ids*. هرگز raise نمی‌کند."""
    dates = set(_affected_dates_for_tasks(task_ids))
    dates |= {_work_date(d) for d in (extra_dates or ()) if d is not None}
    return request_queue_sync(dates)


def sync_queue_for_date(date):
    """بازسازی صف یک تاریخ. برمی‌گرداند (queue_count, diagnostics)."""
    # If inside a queue_sync_scope, register the date for batched flush
    pending = _pending_sync_dates()
    if pending is not None:
        pending.add(date)
        # Return placeholder - actual sync happens at scope flush
        return 0, {}

    result, diagnostics = sync_daily_material_queue_for_dates([date])
    queue_count = result.get(date, 0)
    return queue_count, diagnostics.get(date, {})


def sync_queue_safe(task_ids=None, date=None):
    """پوشش best-effort برای ویوها. هرگز raise نمی‌کند."""
    try:
        if date is not None:
            return sync_queue_for_date(date)
        return sync_queue_for_tasks(task_ids or [])
    except Exception:
        _logger.exception('sync_queue_safe: queue sync failed')
        return (0, {}) if date is not None else []


def _assert_date_not_locked(date):
    """Raise HandoverError if the work date is locked (closed)."""
    from .models import DailyMaterialClosing
    try:
        closing = DailyMaterialClosing.objects.get(work_date=date)
        if closing.status == 'locked':
            raise HandoverError(
                f'این روز قفل شده است (تأیید و قفل توسط {closing.locked_by} در '
                f'{closing.locked_at:%Y-%m-%d %H:%M}). هیچ تحویل/بازگشت/لغو امکان‌پذیر نیست.'
            )
    except DailyMaterialClosing.DoesNotExist:
        pass  # Not locked


def _get_primary_task_and_order(queue):
    """
    Return (production_task, order_item) from queue's sources for StockMovement references.

    Prefers painting/station sources (kind in 'painting', 'station') over rework/carryover.
    Returns the first matching source's task and its order_item.
    """
    source = queue.sources.filter(
        kind__in=('painting', 'station'),
        production_task__isnull=False,
    ).select_related('production_task', 'production_task__order_item').first()

    if source and source.production_task:
        return source.production_task, source.production_task.order_item

    return None, None


# ---------------------------------------------------------------------------
# تحویل و بازگشت
# ---------------------------------------------------------------------------

@transaction.atomic
def execute_daily_delivery(*, queue_id, delivered_by, note='', quantity=None):
    """
    تحویل مواد برای یک ردیف صف — تنها نقطهٔ کسر موجودی بابت تولید.

    quantity=None → باقی‌ماندهٔ نیاز تحویل می‌شود (بار اول با گرد کردن به بسته،
                    بارهای بعد دقیقاً باقی‌مانده).
    quantity=X    → انباردار مقدار دلخواه (کمتر یا بیشتر از نیاز) را تحویل می‌دهد.
    مازاد روی نیاز در excess_consumption می‌آید؛ کسری در روز کاری بعد منتقل می‌شود.
    """
    try:
        queue = DailyMaterialQueue.objects.select_for_update().get(pk=queue_id)
    except DailyMaterialQueue.DoesNotExist:
        raise HandoverError('ردیف صف مواد روزانه یافت نشد.')

    _assert_date_not_locked(queue.work_date)

    if queue.status == 'cancelled':
        raise HandoverError(
            'این ردیف از برنامهٔ روز حذف شده است؛ تا زمانی که نیاز آن در '
            'برنامه نباشد، تحویل ممکن نیست.'
        )
    if queue.status in ('returned', 'closed') or _q2(queue.returned_quantity) > 0:
        raise HandoverError(
            'این ردیف قبلاً تحویل داده شده است و تحویل دوبارهٔ آن ممکن نیست.'
        )

    planned = _q2(queue.planned_quantity)
    delivered_before = _q2(queue.delivered_quantity)
    # Lock raw_material row to prevent race condition on stock check/update
    raw_material = RawMaterial.objects.select_for_update().get(pk=queue.raw_material_id)
    pack = _q2(raw_material.pack_size or 0)
    stock = _q2(raw_material.current_stock)

    if quantity is None:
        if planned <= 0:
            raise HandoverError('مقدار برنامه‌ریزی‌شده برای این ردیف صفر است؛ تحویلی انجام نمی‌شود.')
        remaining = _q2(planned - delivered_before)
        if remaining <= 0:
            raise HandoverError(
                'این ردیف قبلاً تحویل داده شده است. برای تحویل اضافه، مقدار را صریحاً وارد کنید.'
            )
        if delivered_before > 0:
            physical = remaining
        else:
            # First delivery: use suggested delivery (capped at need, leaves one pack)
            physical = _suggested_delivery(remaining, pack, stock)
    else:
        try:
            physical = _q2(quantity)
        except (TypeError, ValueError, ArithmeticError, InvalidOperation):
            raise HandoverError('مقدار تحویل معتبر نیست.')
        if not physical.is_finite() or physical <= 0:
            raise HandoverError('مقدار تحویل باید بزرگ‌تر از صفر باشد.')

    if physical > stock:
        raise HandoverError(
            f'موجودی انبار کافی نیست. برای «{raw_material.name}» '
            f'مقدار تحویل {physical} لازم است اما موجودی {stock} است.'
        )

    # Get primary task and order_item for traceability
    ref_task, ref_order_item = _get_primary_task_and_order(queue)

    StockMovement.objects.create(
        raw_material=raw_material,
        movement_type='consumption',
        quantity=physical,
        daily_queue=queue,
        reference_task=ref_task,
        reference_order_item=ref_order_item,
        created_by=delivered_by,
        note=(note or '')[:255] or f'تحویل روزانه — {raw_material.name} به {queue.worker}',
    )
    queue.delivered_quantity = _q2(delivered_before + physical)
    queue.status = _delivery_status(queue)
    queue.recalculate_consumption()
    queue.save(update_fields=[
        'delivered_quantity', 'status',
        'actual_consumption', 'excess_consumption', 'updated_at',
    ])

    _clear_conflict_if_resolved(queue)

    if queue.status != 'partial':
        _mark_rework_defects_delivered(queue)

    next_date = _shift_working_date(queue.work_date, +1)
    if next_date is not None:
        transaction.on_commit(lambda d=next_date: request_queue_sync([d]))
    return queue


def _mark_rework_defects_delivered(queue):
    """
    وقتی ردیفی که از خرابی آمده تحویل شد، وضعیت همان خرابی‌ها «مواد تحویل شد».

    فقط برای منابعی که واقعاً از خرابی آمده‌اند و سهمشان صفر نیست.
    """
    defects = [
        source.defect
        for source in queue.sources.filter(kind='rework', defect__isnull=False)
        if _q2(source.quantity) > 0
    ]
    if not defects:
        return
    from product.models import ProductionDefect

    ProductionDefect.objects.filter(
        pk__in=[d.pk for d in defects], status='material_requested',
    ).update(status='rework_issued')


@transaction.atomic
def execute_daily_return(*, queue_id, returned_by, returned_quantity, note=''):
    """
    ثبت بازگشت فیزیکی مواد برای یک ردیف صف.

    یک ``StockMovement(return)`` واقعی می‌سازد و موجودی انبار را زیاد می‌کند.

    قوانین حسابداری:
        * جمع برگشتی هرگز از جمع تحویل‌شده بیشتر نمی‌شود، پس
          ``actual_consumption = delivered - returned`` هرگز منفی نمی‌شود؛
        * بازگشت هرگز مصرف را زیاد نمی‌کند، فقط کم می‌کند؛
        * مقدار برنامه‌ریزی‌شده و منابع آن دست‌نخورده می‌مانند؛
        * مادهٔ برگشتی دوباره موجودی عمومی انبار است و برای کارگر امانت یا
          رزروی ساخته نمی‌شود.

    بازگشت جزئی مجاز است: چند بازگشت تا وقتی جمعشان از مقدار تحویل‌شده بیشتر
    نشود ثبت می‌شود. وقتی چیزی برای برگشت نماند، درخواست بعدی رد می‌شود
    (idempotent).

    ردیف صف برای کل عملیات قفل می‌شود، پس دو درخواست هم‌زمان هرگز دو حرکت
    برگشت برای یک ردیف نمی‌سازند.
    """
    try:
        ret = _q2(returned_quantity)
    except (TypeError, ValueError, ArithmeticError, InvalidOperation):
        raise HandoverError('مقدار برگشتی معتبر نیست.')
    if not ret.is_finite() or ret <= 0:
        raise HandoverError('مقدار برگشتی باید بزرگ‌تر از صفر باشد.')

    try:
        queue = DailyMaterialQueue.objects.select_for_update().get(pk=queue_id)
    except DailyMaterialQueue.DoesNotExist:
        raise HandoverError('ردیف صف مواد روزانه یافت نشد.')

    _assert_date_not_locked(queue.work_date)

    if queue.status == 'cancelled':
        raise HandoverError(
            'این ردیف از برنامهٔ روز حذف شده است؛ برگشت برای آن ثبت نمی‌شود.'
        )

    delivered = _q2(queue.delivered_quantity)
    already_returned = _q2(queue.returned_quantity)

    if delivered <= 0:
        raise HandoverError('این ردیف هنوز تحویل نشده است؛ ابتدا تحویل را ثبت کنید.')

    max_returnable = _q2(delivered - already_returned)
    if max_returnable <= 0:
        raise HandoverError('همهٔ مقدار تحویل‌شدهٔ این ردیف قبلاً برگشت داده شده است.')
    if ret > max_returnable:
        raise HandoverError(
            f'مقدار برگشتی ({ret}) نمی‌تواند از مقدار تحویل‌شدهٔ برگشت‌نشده '
            f'({max_returnable}) بیشتر باشد.'
        )

    # Get primary task and order_item for traceability
    ref_task, ref_order_item = _get_primary_task_and_order(queue)

    StockMovement.objects.create(
        raw_material=queue.raw_material,
        movement_type='return',
        quantity=ret,
        daily_queue=queue,
        reference_task=ref_task,
        reference_order_item=ref_order_item,
        created_by=returned_by,
        note=(note or '')[:255] or f'بازگشت روزانه — {queue.raw_material.name} از {queue.worker}',
    )
    queue.returned_quantity = _q2(already_returned + ret)
    queue.status = _delivery_status(queue)
    # planned_quantity و منابع برنامه‌ریزی دست‌نخورده می‌مانند.
    queue.recalculate_consumption()
    queue.save(update_fields=[
        'returned_quantity', 'status',
        'actual_consumption', 'excess_consumption', 'updated_at',
    ])

    _clear_conflict_if_resolved(queue)

    return queue


@transaction.atomic
def execute_auto_return(*, queue_id, returned_by, note=''):
    """
    برگشت خودکار مازاد تحویل در پایان روز کاری.

    وقتی یک ردیف تحویل داده می‌شود، مقدار ``delivered - planned`` مازاد
    محسوب می‌شود؛ این مقدار به‌صورت خودکار در پایان روز برگشت داده می‌شود تا
    مصرف واقعی دقیقاً معادل نیاز برنامه‌ریزی‌شده باشد.

    اگر انباردار مقدار برگشت دستی ثبت کرده باشد، مازاد قبلی کمتر می‌شود؛ هر
    تفاوی بین مجموع برگشت‌ها و «مازاد خودکار» به‌عنوان مصرف اضافه یا کم
    از طریق ``recalculate_consumption`` ثبت می‌شود.

    این تابع idempotent است؛ صدور همزمان آن برای یک ردیف دوباره نمی‌شود.
    """
    try:
        queue = DailyMaterialQueue.objects.select_for_update().get(pk=queue_id)
    except DailyMaterialQueue.DoesNotExist:
        raise HandoverError('ردیف صف مواد روزانه یافت نشد.')

    _assert_date_not_locked(queue.work_date)

    if queue.status == 'cancelled':
        raise HandoverError(
            'این ردیف از برنامهٔ روز حذف شده است؛ برگشت برای آن ثبت نمی‌شود.'
        )

    delivered = _q2(queue.delivered_quantity)
    planned = _q2(queue.planned_quantity)
    already_returned = _q2(queue.returned_quantity)

    if delivered <= 0:
        raise HandoverError('این ردیف هنوز تحویل نشده است؛ ابتدا تحویل را ثبت کنید.')

    actual_consumption = _q2(delivered - already_returned)
    excess = _q2(actual_consumption - planned)
    if excess <= 0:
        return queue

    max_returnable = actual_consumption
    actual_return = _q2(min(excess, max_returnable))

    # Get primary task and order_item for traceability
    ref_task, ref_order_item = _get_primary_task_and_order(queue)

    StockMovement.objects.create(
        raw_material=queue.raw_material,
        movement_type='return',
        quantity=actual_return,
        daily_queue=queue,
        reference_task=ref_task,
        reference_order_item=ref_order_item,
        created_by=returned_by,
        note=(note or '')[:255] or f'برگشت خودکار مازاد تحویل — {queue.raw_material.name}',
    )
    queue.returned_quantity = _q2(already_returned + actual_return)
    queue.status = _delivery_status(queue)
    queue.recalculate_consumption()
    queue.save(update_fields=[
        'returned_quantity', 'status',
        'actual_consumption', 'excess_consumption', 'updated_at',
    ])

    _clear_conflict_if_resolved(queue)

    return queue


@transaction.atomic
def execute_batch_auto_return(*, date, returned_by, note=''):
    """
    برگشت خودکارِِ انبوه برای تمام ردیف‌های یک روز.

    تمام ردیف‌هایی که:
      - delivered > planned (مازاد دارند)
      - returned_quantity == 0 (هنوز برگشتی ندارند)
    را در یک تراکنش پردازش می‌کند.

    خروجی: (processed_count, total_returned_quantity, list_of_queue_ids)
    """
    _assert_date_not_locked(date)

    qs = DailyMaterialQueue.objects.select_for_update().filter(
        work_date=date,
        status__in=('delivered', 'partial'),
        returned_quantity=0,
    )
    # Filter in Python to check excess > 0
    processed = 0
    total_returned = Decimal('0')
    processed_ids = []

    for queue in qs:
        delivered = _q2(queue.delivered_quantity)
        planned = _q2(queue.planned_quantity)
        if delivered <= planned:
            continue

        excess = _q2(delivered - planned)
        if excess <= 0:
            continue

        # Get primary task and order_item for traceability
        ref_task, ref_order_item = _get_primary_task_and_order(queue)

        StockMovement.objects.create(
            raw_material=queue.raw_material,
            movement_type='return',
            quantity=excess,
            daily_queue=queue,
            reference_task=ref_task,
            reference_order_item=ref_order_item,
            created_by=returned_by,
            note=(note or '')[:255] or f'برگشت خودکار انبوه — {queue.raw_material.name}',
        )
        queue.returned_quantity = _q2(queue.returned_quantity + excess)
        queue.status = _delivery_status(queue)
        queue.recalculate_consumption()
        queue.save(update_fields=[
            'returned_quantity', 'status',
            'actual_consumption', 'excess_consumption', 'updated_at',
        ])
        _clear_conflict_if_resolved(queue)

        processed += 1
        total_returned += excess
        processed_ids.append(queue.pk)

    return processed, total_returned, processed_ids


@transaction.atomic
def cancel_daily_queue(*, queue_id, cancelled_by, note=''):
    """
    لغو/کنار گذاشتن یک ردیف صف روزانه (pending).

    برای مواردی که کارگر غایب است یا نیازی به مواد نیست. ردیف به وضعیت
    'cancelled' تغییر می‌کند و planned_quantity حفظ می‌شود برای گزارش‌گیری.
    هیچ حرکتی در انبار ثبت نمی‌شود.

    اگر ردیف قبلاً تحویل/برگشت داشته باشد، لغو امکان‌پذیر نیست.
    """
    try:
        queue = DailyMaterialQueue.objects.select_for_update().get(pk=queue_id)
    except DailyMaterialQueue.DoesNotExist:
        raise HandoverError('ردیف صف مواد روزانه یافت نشد.')

    _assert_date_not_locked(queue.work_date)

    if queue.status == 'cancelled':
        raise HandoverError('این ردیف قبلاً لغو شده است.')

    if queue.has_transaction:
        raise HandoverError(
            'این ردیف تحویل/برگشت دارد؛ برای لغو ابتدا تراکنش‌ها را حذف کنید.'
        )

    queue.status = 'cancelled'
    queue.note = (note or '')[:255]
    queue.save(update_fields=['status', 'note', 'updated_at'])

    # Trigger re-sync for next working day (carryover cleanup)
    next_date = _shift_working_date(queue.work_date, +1)
    if next_date is not None:
        transaction.on_commit(lambda d=next_date: request_queue_sync([d]))

    return queue


@transaction.atomic
def create_stock_adjustment(*, raw_material, quantity, created_by, note=''):
    """
    اصلاح دستی موجودی (مثلاً شمارش نادرست).

    عمداً فقط حرکت ``adjustment`` (افزایش) یا ``adjust_out`` (کاهش) می‌سازد و
    هیچ راهی برای ثبت «مصرف» دستی وجود ندارد: مصرف فقط از صف روزانه ثبت
    می‌شود تا موجودی هرگز دوبار کم نشود.

    مقدار مثبت موجودی را زیاد و مقدار منفی آن را کم می‌کند.
    """
    amount = _q2(quantity)
    if not amount.is_finite() or amount == 0:
        raise HandoverError('مقدار اصلاحیه باید بزرگ‌تر از صفر باشد.')

    if amount > 0:
        movement_type = 'adjustment'
        text = note or 'اصلاح دستی موجودی (افزایش)'
    else:
        movement_type = 'adjust_out'
        text = note or 'اصلاح دستی موجودی (کاهش)'

    return StockMovement.objects.create(
        raw_material=raw_material,
        movement_type=movement_type,
        quantity=abs(amount),
        created_by=created_by,
        note=text[:255],
    )


@transaction.atomic
def reverse_purchase(*, movement_id, reversed_by, note=''):
    """
    ابطال یک حرکت «خرید/ورود» (purchase) با ایجاد یک حرکت معکوس از نوع
    ``purchase_reversal``.

    قوانین:
    - فقط حرکات با movement_type='purchase' قابل ابطال هستند.
    - یک purchase فقط یک‌بار قابل ابطال است (چک می‌کند reversals.exists()).
    - موجودی انبار باید برای کسر مقدار purchase_reversal کافی باشد.
    - reason/note الزامی است (حداقل ۳ کاراکتر) برای ردیابی.
    - نکته: movement_type='purchase_reversal' در STOCK_DECREASING_TYPES است، پس موجودی کم می‌شود.
    """
    if not note or len(note.strip()) < 3:
        raise HandoverError('دلیل ابطال باید حداقل ۳ کاراکتر باشد.')

    try:
        original = StockMovement.objects.select_for_update().get(pk=movement_id)
    except StockMovement.DoesNotExist:
        raise HandoverError('حرکت انبار یافت نشد.')

    if original.movement_type != 'purchase':
        raise HandoverError('فقط حرکات «خرید/ورود» قابل ابطال هستند.')

    if original.reversals.exists():
        raise HandoverError('این دریافت قبلاً ابطال شده است.')

    # Check stock sufficiency
    if original.raw_material.current_stock < original.quantity:
        raise HandoverError(
            f'موجودی انبار برای ابطال این دریافت کافی نیست. '
            f'موجودی فعلی: {original.raw_material.current_stock}, '
            f'مقدار دریافت: {original.quantity}'
        )

    reversal = StockMovement.objects.create(
        raw_material=original.raw_material,
        movement_type='purchase_reversal',
        quantity=original.quantity,
        reverses=original,
        created_by=reversed_by,
        note=(note or '')[:255] or f'ابطال دریافت #{original.pk}',
    )

    return reversal


def is_need_delivered_by_queue(task, raw_material):
    """
    آیا نیاز این تسک برای این ماده از صف مواد روزانه **تحویل شده** است؟

    خواندنی است و هیچ نوشتنی انجام نمی‌دهد. پاسخ ``True`` فقط وقتی قطعی است
    که هم تسک و هم ماده در یک ردیف صفِ تحویل‌شده منبع باشند.

    چرا همان ماده هم بخشی از شرط است: مصرف دستی برای ماده‌ای دیگر هرگز تکرار
    تحویل صف نیست؛ صرفِ وجود صف برای یک ماده در یک روز نباید مصرف دستیِ
    مستقلِ معتبر را بلاک کند.
    """
    from .models import DailyMaterialQueueSource

    if task is None or raw_material is None:
        return False
    return DailyMaterialQueueSource.objects.filter(
        production_task=task,
        raw_material=raw_material,
        queue__delivered_quantity__gt=0,
    ).exists()


# نام قدیمی‌تر برای همان گارد؛ نگه داشته شده تا فراخوان‌های موجود نشکنند.
is_paint_need_delivered_by_queue = is_need_delivered_by_queue


# ---------------------------------------------------------------------------
# گزارش صف روزانه (فقط خواندنی)
#
# مصرف واقعی از فیلد ``actual_consumption`` خودِ ردیف صف خوانده می‌شود — همان
# فیلدی که ``recalculate_consumption`` از «تحویل منهای برگشت» نگه می‌دارد —
# تا هیچ منطق موازی یا تعریف دومی از مصرف ایجاد نشود.
#
# منبع حقیقت موجودی انبار همچنان ``StockMovement`` است؛ این گزارش به دفتر
# حرکات دست نمی‌زند و StockMovement جدیدی نمی‌سازد. برگشتی‌ها صرفاً موجودی
# عمومی انبار هستند و در این گزارش هرگز به‌عنوان مصرف شمرده نمی‌شوند.
# ---------------------------------------------------------------------------

def daily_queue_filtered_queryset(date, *, worker_id=None, material_id=None,
                                   status=None, include_cancelled=False):
    """
    queryset خام صف مواد روزانه برای *date* با فیلترهای اختیاری.

    از این تابع در صفحهٔ انبار و هر گزارش دیگری استفاده می‌شود تا تعریف
    «یک ردیف صف» در یک نقطه بماند.
    """
    qs = DailyMaterialQueue.objects.filter(work_date=date)

    if worker_id:
        qs = qs.filter(worker_id=worker_id)
    if material_id:
        qs = qs.filter(raw_material_id=material_id)
    if status:
        qs = qs.filter(status=status)
    if not include_cancelled and status != 'cancelled':
        # ردیف لغوشده نیاز فعالی ندارد و در گزارش عملیات روزانه گمراه‌کننده است.
        # اگر کاربر صریحاً «لغو شده» را فیلتر کرده باشد، انتخاب او معتبر است.
        qs = qs.exclude(status='cancelled')

    return qs


def daily_queue_summary(qs):
    """
    جمع روزانه از یک queryset صف — فقط خواندنی.

    مجموع planned / delivered / returned / actual از همان فیلدهای ذخیره‌شدهٔ
    خود ردیف صف می‌آید (نه محاسبهٔ موازی). شمارش وضعیت‌ها و تعارض‌ها با
    ``filter=`` داخل همان یک ``aggregate`` انجام می‌شود تا برای هر گزارش
    فقط یک کوئری اجرا شود.
    """
    from django.db.models import Count, Q, Sum

    totals = qs.aggregate(
        items=Count('id'),
        total_planned=Sum('planned_quantity'),
        total_delivered=Sum('delivered_quantity'),
        total_returned=Sum('returned_quantity'),
        total_actual=Sum('actual_consumption'),
        total_excess=Sum('excess_consumption'),
        pending_count=Count('id', filter=Q(status='pending')),
        delivered_count=Count('id', filter=Q(status='delivered')),
        returned_count=Count('id', filter=Q(status='returned')),
        conflict_count=Count('id', filter=Q(has_plan_conflict=True)),
    )

    return {
        'items': totals['items'] or 0,
        'total_planned': _q2(totals['total_planned'] or 0),
        'total_delivered': _q2(totals['total_delivered'] or 0),
        'total_returned': _q2(totals['total_returned'] or 0),
        'total_actual': _q2(totals['total_actual'] or 0),
        'total_excess': _q2(totals['total_excess'] or 0),
        'pending_count': totals['pending_count'] or 0,
        'delivered_count': totals['delivered_count'] or 0,
        'returned_count': totals['returned_count'] or 0,
        'conflict_count': totals['conflict_count'] or 0,
    }


def daily_material_report(date, *, worker_id=None, material_id=None, status=None):
    """
    گزارش مصرف مواد روزانه برای یک تاریخ کاری.

    خروجی: dict با کلیدهای ``rows`` (صف آمادهٔ نمایش/صفحه‌بندی) و ``summary``.
    هیچ نوشتنی در دیتابیس انجام نمی‌شود و planned_quantity دست‌نخورده می‌ماند.
    """
    base = daily_queue_filtered_queryset(
        date, worker_id=worker_id, material_id=material_id, status=status,
    )
    summary = daily_queue_summary(base)

    rows_qs = (
        base
        .select_related('worker', 'raw_material', 'raw_material__category')
        .order_by(
            'worker__first_name', 'worker__last_name', 'worker__username',
            'raw_material__name',
        )
    )

    # Get diagnostics from sync
    _, diagnostics = sync_queue_for_date(date)

    return {
        'rows': rows_qs,
        'summary': summary,
        'diagnostics': diagnostics,
    }


def daily_queue_action_state(queue):
    """
    وضعیت اقدام‌های ممکن روی یک ردیف صف، برای نمایش در UI.

    فقط می‌خواند و هیچ قاعدهٔ جدیدی نمی‌سازد: شرط‌ها دقیقاً همان‌هایی هستند که
    ``execute_daily_delivery`` و ``execute_daily_return`` اعمال می‌کنند. UI نباید
    این قاعده را از روی متن Badge یا با محاسبهٔ دستی تکرار کند.
    """
    planned = _q2(queue.planned_quantity)
    delivered = _q2(queue.delivered_quantity)
    already_returned = _q2(queue.returned_quantity)
    max_returnable = _q2(max(delivered - already_returned, Decimal('0')))
    remaining = _q2(max(planned - delivered, Decimal('0')))
    no_returns = already_returned <= 0

    can_deliver = (
        queue.status in ('pending', 'partial')
        and no_returns
        and planned > 0
        and remaining > 0
    )
    can_add_extra = (
        queue.status in ('partial', 'delivered')
        and no_returns
        and delivered > 0
    )
    can_return = (
        queue.status not in ('cancelled', 'returned')
        and delivered > 0
        and max_returnable > 0
    )

    pack = _q2(queue.raw_material.pack_size or 0)
    stock = _q2(queue.raw_material.current_stock)
    suggested = _suggested_delivery(remaining, pack, stock)
    auto_returnable = _q2(max(delivered - planned, Decimal('0')))
    can_auto_return = (
        queue.status in ('delivered', 'partial')
        and no_returns
        and auto_returnable > 0
        and max_returnable > 0
    )

    # Can cancel: pending status, no transactions, planned > 0
    can_cancel = (
        queue.status == 'pending'
        and not queue.has_transaction
        and planned > 0
    )

    return {
        'status': queue.status,
        'status_display': queue.get_status_display(),
        'can_deliver': can_deliver,
        'can_return': can_return,
        'max_returnable': str(max_returnable),
        'remaining': str(remaining),
        'can_add_extra': can_add_extra,
        'suggested_delivery': str(suggested),
        'auto_returnable': str(auto_returnable),
        'can_auto_return': can_auto_return,
        'can_cancel': can_cancel,
    }


# ---------------------------------------------------------------------------
# کنترل و بستن روز (فقط خواندنی، به‌جز سند تأیید)
# ---------------------------------------------------------------------------

def daily_closing_problems(qs):
    """
    ردیف‌هایی که بستن روز را ناامن می‌کنند — کاملاً فقط‌خواندنی.

    هیچ قاعدهٔ کسب‌وکار جدیدی اختراع نمی‌شود؛ هر سه دسته مستقیماً از وضعیت و
    فیلدهای موجود ``DailyMaterialQueue`` مشتق می‌شود:

    * ``conflict``           → ``has_plan_conflict``
    * ``incomplete``         → نیاز برنامه‌ریزی‌شده با تحویل/برگشت برآورده نشده:
                               - pending با planned > 0
                               - delivered > 0 و returned == 0 (تحویل شده اما برگشت نداشته)
    * ``inconsistent``       → وضعیت ذخیره‌شده با مقدارهای تحویل/برگشت نمی‌خواند
    * ``excess_delivered``   → تحویل > برنامه (مازاد دارد، باید برگشت خودکار شود)
    * ``overdue_partial``    → ردیف partial که از روز قبل carryover شده (کسری معوق)

    خروجی: لیستی از ``{'queue': row, 'reasons': [...]}``.
    """
    problems = []
    for queue in qs:
        planned = _q2(queue.planned_quantity)
        delivered = _q2(queue.delivered_quantity)
        returned = _q2(queue.returned_quantity)

        reasons = set()
        if queue.has_plan_conflict:
            reasons.add('conflict')

        # incomplete: planned not fully delivered/returned
        # - pending with planned > 0
        # - delivered > 0 but returned == 0 (delivered without return)
        if queue.status == 'pending' and planned > 0:
            reasons.add('incomplete')
        elif delivered > 0 and returned == 0 and queue.status in ('delivered', 'partial'):
            reasons.add('incomplete')

        # excess_delivered: delivered > planned (surplus to auto-return)
        if delivered > planned:
            reasons.add('excess_delivered')

        # overdue_partial: partial from carryover (source kind == 'carryover')
        if queue.status == 'partial':
            has_carryover_source = queue.sources.filter(kind='carryover').exists()
            if has_carryover_source:
                reasons.add('overdue_partial')

        if returned > delivered:
            reasons.add('inconsistent')
        if queue.status in ('delivered', 'returned') and not queue.has_transaction:
            reasons.add('inconsistent')
        if queue.status == 'returned' and returned < delivered:
            reasons.add('inconsistent')
        if queue.status == 'partial' and not queue.has_transaction:
            reasons.add('inconsistent')
        if _q2(queue.actual_consumption) != _q2(queue.computed_actual_consumption):
            reasons.add('inconsistent')

        if reasons:
            problems.append({'queue': queue, 'reasons': sorted(reasons)})

    return problems


def daily_closing_status(date, *, worker_id=None, material_id=None):
    """
    «کنترل و بستن روز» برای یک تاریخ کاری — فقط خواندنی.

    هیچ نوشتنی انجام نمی‌دهد: نه ``StockMovement`` می‌سازد، نه تحویل/بازگشتی
    اجرا می‌کند و نه مقداری از صف را تغییر می‌دهد.

    وضعیت روز:
        ``closable``      → هیچ ردیف کنترل‌نشده‌ای وجود ندارد
        ``needs_review``  → دست‌کم یک conflict / ردیف ناقص / تراکنش ناسازگار
    """
    base = daily_queue_filtered_queryset(
        date, worker_id=worker_id, material_id=material_id,
    )
    summary = daily_queue_summary(base)

    problem_rows = (
        base
        .select_related('worker', 'raw_material')
        .order_by('raw_material__name', 'worker__username')
    )
    problems = daily_closing_problems(problem_rows)

    from .models import DailyMaterialClosing

    return {
        'summary': summary,
        'problems': problems,
        'status': 'closable' if not problems else 'needs_review',
        'closable': not problems,
        'problem_count': len(problems),
        'incomplete_count': sum(1 for p in problems if 'incomplete' in p['reasons']),
        'conflict_count': sum(1 for p in problems if 'conflict' in p['reasons']),
        'inconsistent_count': sum(1 for p in problems if 'inconsistent' in p['reasons']),
        'excess_delivered_count': sum(1 for p in problems if 'excess_delivered' in p['reasons']),
        'overdue_partial_count': sum(1 for p in problems if 'overdue_partial' in p['reasons']),
        'locked': DailyMaterialClosing.is_locked(date),
    }


def get_daily_closing(date):
    """سند تأیید روز، اگر ثبت شده باشد (فقط خواندنی)."""
    from .models import DailyMaterialClosing

    return DailyMaterialClosing.objects.filter(work_date=date).first()


@transaction.atomic
def confirm_daily_closing(*, date, closed_by, note='', force=False):
    """
    ثبت «روز بررسی و تأیید شد».

    قوانین:
        * روز دارای مشکل کنترل‌نشده بسته نمی‌شود (مگر با force=True);
        * یک روز دوبار بسته نمی‌شود;
        * هیچ ``StockMovement`` ایجاد نمی‌شود;
        * هیچ مقدار ``DailyMaterialQueue`` تغییر نمی‌کند.

    با force=True (فقط ادمین)، تعارض‌های برنامه (conflict) نادیده گرفته می‌شوند
    اما تراکنش‌های ناسازگار (inconsistent)، ردیف‌های ناقص (incomplete)،
    مازادهای تحویل (excess_delivered) و کسری‌های معوق (overdue_partial) همچنان
    مانع بستن می‌شوند.
    """
    from .models import DailyMaterialClosing

    control = daily_closing_status(date)

    if not control['closable'] and not force:
        raise HandoverError(
            f'این روز قابل بستن نیست: {control["problem_count"]} ردیف '
            'نیازمند بررسی است (تعارض، ردیف ناقص، تراکنش ناسازگار، مازاد تحویل یا کسری معوق). '
            'برای بستن با تعارض، از پارامتر force استفاده کنید.'
        )

    # With force, still reject inconsistent/incomplete/excess/overdue
    if force and not control['closable']:
        # Check if only conflicts are the issue
        has_only_conflicts = all(
            set(p['reasons']).issubset({'conflict'})
            for p in control['problems']
        )
        if not has_only_conflicts:
            raise HandoverError(
                'باز هم قابل بستن نیست: تراکنش ناسازگار، ردیف ناقص، مازاد تحویل یا کسری معوق وجود دارد. '
                'فقط تعارض برنامه (conflict) قابل نادیده گرفتن است.'
            )

    if DailyMaterialClosing.objects.filter(work_date=date).exists():
        raise HandoverError('این روز قبلاً بررسی و تأیید شده است.')

    try:
        closing = DailyMaterialClosing.objects.create(
            work_date=date,
            status='locked',  # Auto-lock on confirm
            closed_by=closed_by,
            locked_by=closed_by,
            locked_at=timezone.now(),
            note=(note or '')[:255],
        )
    except IntegrityError:
        raise HandoverError('این روز قبلاً بررسی و تأیید شده است.')

    return closing


def preview_daily_delivery(queue_id):
    """
    پیش‌نمایش فقط‌خواندنیِ کاری که ``execute_daily_delivery`` انجام می‌دهد.
    """
    try:
        queue = DailyMaterialQueue.objects.get(pk=queue_id)
    except DailyMaterialQueue.DoesNotExist:
        raise HandoverError('ردیف صف مواد روزانه یافت نشد.')

    planned = _q2(queue.planned_quantity)
    pack = _q2(queue.raw_material.pack_size or 0)
    stock = _q2(queue.raw_material.current_stock)

    delivered = _q2(queue.delivered_quantity)
    remaining = _q2(max(planned - delivered, Decimal('0')))
    if delivered > 0:
        physical = remaining
    else:
        physical = _suggested_delivery(remaining, pack, stock)

    open_remainder = _q2(stock % pack) if pack > 0 else ZERO
    from_open = _q2(min(physical, open_remainder))
    from_new_pack = _q2(physical - from_open)
    remainder_after = _q2((stock - physical) % pack) if pack > 0 else ZERO
    # تعداد بستهٔ جدیدی که باید باز شود (نه بستهٔ باز موجود)
    packs = _packs_counted(from_new_pack, pack) if pack > 0 else 0

    suggested_delivery = _suggested_delivery(
        _q2(max(planned - delivered, Decimal('0'))), pack, stock,
    )

    return {
        'queue': queue,
        'planned': planned,
        'pack_size': pack,
        'packs': packs,
        'physical': physical,
        'stock': stock,
        'enough': physical <= stock,
        'name': queue.raw_material.name,
        'unit': queue.raw_material.get_unit_display(),
        'remaining': remaining,
        'open_remainder': open_remainder,
        'from_open': from_open,
        'from_new_pack': from_new_pack,
        'remainder_after': remainder_after,
        **daily_queue_action_state(queue),
        'suggested_delivery': suggested_delivery,
    }


def queue_sources(queue_id):
    """
    ردیف‌های منبع یک صف، آمادهٔ نمایش در مودول «این نیاز از کجا آمده».

    فقط خواندنی. هر منبع یک ردیف سادهٔ dict است تا ویو بدون ساخت HTML در ردیفه.
    """
    try:
        queue = DailyMaterialQueue.objects.get(pk=queue_id)
    except DailyMaterialQueue.DoesNotExist:
        raise HandoverError('ردیف صف مواد روزانه یافت نشد.')

    rows = []
    for source in (
        queue.sources
        .select_related(
            'production_task', 'painting_stage', 'painting_stage__process',
            'production_task__order_item',
            'defect', 'defect__order', 'defect__order_item__product',
            'carryover_from',
        )
        .order_by('kind', 'id')
    ):
        task = source.production_task
        stage = source.painting_stage
        process = stage.process if stage and stage.process_id else None

        if source.kind == 'carryover':
            origin = source.carryover_from
            rows.append({
                'kind': 'carryover',
                'kind_display': 'انتقال کسری',
                'label': f'کسری ردیف #{origin.pk}' if origin else 'کسری روز قبل',
                'detail': (
                    f'{origin.work_date} — تحویل {_q2(origin.delivered_quantity)} '
                    f'از {_q2(origin.planned_quantity)}' if origin else ''
                ),
                'quantity': _q2(source.quantity),
                'task_label': '',
                'stage_name': '',
                'process_name': '',
                'color_part': '',
                'order_item': '',
            })
            continue

        if source.kind == 'rework':
            defect = source.defect
            rows.append({
                'kind': 'rework',
                'kind_display': 'جبران خرابی',
                'label': f'خرابی #{defect.id}' if defect else '—',
                'detail': (
                    f'{defect.get_color_part_display()} — '
                    f'{defect.quantity} عدد' if defect and defect.color_part else ''
                ),
                'quantity': _q2(source.quantity),
                # کلیدهای تفصیلی خالی‌اند چون منبعِ این ردیف تسک نیست.
                'task_label': '',
                'stage_name': '',
                'process_name': '',
                'color_part': defect.color_part if defect else '',
                'order_item': f'سفارش #{defect.order_id}' if defect and defect.order_id else '',
            })
            continue

        rows.append({
            'kind': source.kind,
            'kind_display': (
                'ایستگاه نقاشی' if source.kind == 'painting' else 'سایر ایستگاه‌ها'
            ),
            'label': task.get_station_name_display() if task else '—',
            'detail': (
                f'{process.name} / {stage.name}'
                if process and stage
                else (task.get_station_name_display() if task else '')
            ),
            'quantity': _q2(source.quantity),
            'task_label': f'تسک #{task.pk}' if task else '',
            'stage_name': stage.name if stage else '',
            'process_name': process.name if process else '',
            'color_part': task.color_part if task else '',
            'order_item': (
                f'سفارش #{task.order_id} / آیتم #{task.order_item_id}'
                if task and task.order_id and task.order_item_id
                else ''
            ),
            'task_id': task.pk if task else None,
        })

    return {
        'queue': queue,
        'worker': queue.worker.get_full_name() or queue.worker.username,
        'material': queue.raw_material.name,
        'planned_quantity': _q2(queue.planned_quantity),
        'delivered_quantity': _q2(queue.delivered_quantity),
        'returned_quantity': _q2(queue.returned_quantity),
        'actual_consumption': _q2(queue.actual_consumption),
        'rows': rows,
        # وضعیت اقدام‌های ممکن همین‌جا می‌آید تا مودال «بازگشت» مجبور نباشد
        # قاعدهٔ «حداکثر قابل برگشت» را در جاوااسکریپت تکرار کند.
        **daily_queue_action_state(queue),
    }