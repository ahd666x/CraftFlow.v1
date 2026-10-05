"""
منطق تحویل گروهی مواد اولیه از انبار به تولید.

قاعدهٔ محاسبه برای هر ماده اولیه (پس از جمع‌زدن همهٔ ردیف‌های انتخاب‌شده):

    need          = جمع مقدارهای انتخاب‌شده
    leftover      = باقی‌ماندهٔ فعلی این ماده در سالن تولید
    from_leftover = min(leftover, need)
    from_stock    = need - from_leftover              (مقداری که باید از انبار بیاید)
    packs         = ceil(from_stock / pack_size)      (اگر pack_size > 0)
    physical      = packs * pack_size                 (خروج فیزیکی واقعی از انبار)
    new_leftover  = leftover - from_leftover + (physical - from_stock)

ثبت در دفتر انبار (StockMovement) طوری است که هم «گزارش مصرف مواد» و هم «موجودی انبار» درست بماند:

  * برای هر درخواست، دقیقاً مقدار همان درخواست به‌عنوان «مصرف» و با ارجاع به تسک/آیتم ثبت می‌شود
    (گزارش مصرف مواد فقط همین‌ها را می‌بیند).
  * اختلاف بین «خروج فیزیکی» و «جمع نیاز» (= تغییر باقی‌مانده سالن) در یک حرکت بدون ارجاع ثبت می‌شود:
      - باقی‌مانده زیاد شد  -> یک «مصرف» بدون ارجاع (قوطیِ باز شده در سالن)
      - باقی‌مانده کم شد    -> یک «اصلاحیه» مثبت (این مقدار قبلاً از انبار کسر شده بود)
    بنابراین موجودی انبار همیشه برابر با «خروج فیزیکی» کم می‌شود، نه بیشتر و نه کمتر.

«leftover» دو منبع دارد و هر دو از همین فرمول استفاده می‌کنند:

  * held_by = None  -> منبع، MaterialLeftover است: باقی‌ماندهٔ «سالن تولید» (قدیمی)
  * held_by = User  -> منبع، MaterialCustody است: باقی‌ماندهٔ بازِ «همان کارگر» (مدل امانت)

تفاوت فقط کلید ذخیره‌سازی است ((raw_material) در برابر (raw_material, held_by))؛
محاسبات و ثبت StockMovement در هر دو حالت یکسان است. بنابراین
«گزارش مصرف مواد» و «موجودی انبار» در هر دو مسیر درست می‌مانند.

بازگشت پایان روز (return_custody) هیچ StockMovement نمی‌سازد، چون چیزی
فیزیکاً وارد انبار نشده؛ فقط دفتر امانت به‌روز و یک سند MaterialCustodyReturn ثبت می‌شود.
"""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import contextlib
import threading

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    CustodyConsumption,
    MaterialCustody,
    MaterialCustodyReturn,
    MaterialHandover,
    MaterialHandoverLine,
    MaterialIssue,
    MaterialLeftover,
    RawMaterial,
    StockMovement,
)

ZERO = Decimal('0.00')
CENT = Decimal('0.01')


class HandoverError(Exception):
    """خطایی که پیامش مستقیماً به کاربر انبار نمایش داده می‌شود."""


def q2(value):
    return Decimal(value).quantize(CENT)


# ---------------------------------------------------------------------------
# بارگذاری و اعتبارسنجی
# ---------------------------------------------------------------------------

def _load_issues(items, lock=False):
    """
    items: list[(issue_id, Decimal quantity)]
    خروجی: list[(MaterialIssue, Decimal)] به همان ترتیب ورودی.
    """
    ids = sorted({issue_id for issue_id, _ in items})
    qs = (
        MaterialIssue.objects
        .select_related('raw_material', 'task', 'order_item', 'defect')
        .filter(pk__in=ids)
        .order_by('pk')
    )
    if lock:
        qs = qs.select_for_update(of=('self',))
    issues = {issue.pk: issue for issue in qs}

    result = []
    for issue_id, qty in items:
        issue = issues.get(issue_id)
        if issue is None:
            raise HandoverError(f'درخواست #{issue_id} یافت نشد.')
        if issue.status not in ('requested', 'partial'):
            raise HandoverError(f'درخواست #{issue_id} قبلاً تعیین تکلیف شده است.')
        remaining = issue.requested_quantity - issue.issued_quantity
        if qty <= 0:
            raise HandoverError(f'مقدار تحویل درخواست #{issue_id} باید بزرگ‌تر از صفر باشد.')
        if qty > remaining:
            raise HandoverError(f'مقدار تحویل درخواست #{issue_id} بیش از مانده‌ی آن ({remaining}) است.')
        result.append((issue, qty))
    return result


# ---------------------------------------------------------------------------
# منبع باقی‌مانده: سالن (legacy) یا امانتِ کارگر (custody)
# ---------------------------------------------------------------------------

def _custody_pool(raw_ids, held_by):
    """
    dict[raw_material_id -> Decimal] از MaterialCustody برای یک کارگر مشخص.
    بدون قفل — فقط برای پیش‌نمایش (preview) استفاده می‌شود.
    """
    if held_by is None:
        return {}
    return {
        row.raw_material_id: row.quantity
        for row in MaterialCustody.objects.filter(
            held_by=held_by, raw_material_id__in=list(raw_ids)
        )
    }


def _lock_custody_rows(raw_ids, held_by):
    """
    ساخت/قفل ردیف‌های امانت یک کارگر.

    الگو عیناً از قفل MaterialLeftover در execute_handover کپی شده:
    قفل ردیف نقش mutex دارد تا دو انباردار هم‌زمان یک بسته را دوبار
    به یک کارگر تحویل ندهند و موجودی امانت را دوبار کم نکنند.
    """
    for raw_id in raw_ids:
        MaterialCustody.objects.get_or_create(
            raw_material_id=raw_id, held_by=held_by, defaults={'quantity': ZERO}
        )
    return {
        row.raw_material_id: row
        for row in MaterialCustody.objects.select_for_update()
        .filter(held_by=held_by, raw_material_id__in=list(raw_ids))
        .order_by('raw_material_id')
    }


# ---------------------------------------------------------------------------
# محاسبهٔ برنامهٔ تحویل (بدون نوشتن در دیتابیس)
# ---------------------------------------------------------------------------

def build_plans(issues_qty, leftovers, *, held_by=None):
    """
    issues_qty: list[(MaterialIssue, Decimal)]
    leftovers : dict[raw_material_id -> Decimal]
    held_by   : اگر داده شود، `leftovers` باید مقدار امانتِ همان کارگر باشد و
                خروجی، منبعِ باقی‌مانده را به‌عنوان «امانت کارگر» علامت می‌زند.
    خروجی: list[dict] — یک دیکشنری برای هر ماده اولیه.
    """
    grouped = OrderedDict()
    for issue, qty in issues_qty:
        raw = issue.raw_material
        entry = grouped.setdefault(raw.pk, {'raw': raw, 'need': ZERO, 'issue_ids': []})
        entry['need'] += qty
        entry['issue_ids'].append(issue.pk)

    plans = []
    for raw_id, entry in grouped.items():
        raw = entry['raw']
        need = q2(entry['need'])
        leftover = q2(leftovers.get(raw_id, ZERO))
        pack = q2(raw.pack_size or ZERO)

        from_leftover = min(leftover, need)
        from_stock = need - from_leftover

        if from_stock > 0 and pack > 0:
            packs = int((from_stock / pack).to_integral_value(rounding=ROUND_CEILING))
            physical = pack * packs
        else:
            packs = 0
            physical = from_stock

        new_leftover = leftover - from_leftover + (physical - from_stock)
        stock = q2(raw.current_stock)

        plans.append({
            'raw': raw,
            'raw_material_id': raw_id,
            'need': need,
            'leftover': leftover,
            'from_leftover': from_leftover,
            'from_stock': from_stock,
            'pack_size': pack,
            'packs': packs,
            'physical': physical,
            'new_leftover': new_leftover,
            'stock': stock,
            'enough': stock >= physical,
            'issue_ids': entry['issue_ids'],
            # --- افزوده‌شده برای مدل امانت (کلیدهای قبلی دست‌نخورده) ---
            'held_by': held_by,
            'custody_source': held_by is not None,
        })
    return plans


def plan_to_dict(plan):
    raw = plan['raw']
    return {
        'raw_material_id': plan['raw_material_id'],
        'name': raw.name,
        'unit': raw.get_unit_display(),
        'pack_size': str(plan['pack_size']),
        'need': str(plan['need']),
        'leftover': str(plan['leftover']),
        'from_leftover': str(plan['from_leftover']),
        'from_stock': str(plan['from_stock']),
        'packs': plan['packs'],
        'physical': str(plan['physical']),
        'new_leftover': str(plan['new_leftover']),
        'stock': str(plan['stock']),
        'enough': plan['enough'],
        'issue_ids': plan['issue_ids'],
        # --- افزوده‌شده برای مدل امانت ---
        'custody_source': plan.get('custody_source', False),
        'held_by': plan['held_by'].get_full_name() or plan['held_by'].username
        if plan.get('held_by') else '',
        'custody': str(plan['leftover']),
        'from_custody': str(plan['from_leftover']),
        'new_custody': str(plan['new_leftover']),
    }


def preview_handover(items, held_by=None):
    """پیش‌نمایش خلاصهٔ تحویل برای نمایش در صفحه (قفل و نوشتنی ندارد)."""
    issues_qty = _load_issues(items, lock=False)
    raw_ids = {issue.raw_material_id for issue, _ in issues_qty}
    if held_by is not None:
        leftovers = _custody_pool(raw_ids, held_by)
    else:
        leftovers = {
            row.raw_material_id: row.quantity
            for row in MaterialLeftover.objects.filter(raw_material_id__in=raw_ids)
        }
    return [plan_to_dict(p) for p in build_plans(issues_qty, leftovers, held_by=held_by)]


# ---------------------------------------------------------------------------
# ثبت نهایی
# ---------------------------------------------------------------------------

def _create_issue_movement(issue, qty, issued_by, handover):
    suffix = f'تحویل شماره {handover.id}'

    if issue.task_id:
        return StockMovement.objects.create(
            raw_material=issue.raw_material,
            movement_type='consumption',
            quantity=qty,
            reference_task=issue.task,
            created_by=issued_by,
            fulfilled_issue=issue,
            note=f'تحویل انبار #{issue.id} — {issue.get_purpose_display()} — {suffix}',
        )

    item = issue.order_item
    if item is None and issue.defect_id:
        item = issue.defect.order_item

    if item is not None:
        note = f'تحویل انبار #{issue.id} — نقاشی — سفارش {item.order_id}/آیتم {item.id} — {suffix}'
    else:
        note = f'تحویل انبار #{issue.id} — {issue.get_purpose_display()} — {suffix}'

    return StockMovement.objects.create(
        raw_material=issue.raw_material,
        movement_type='consumption',
        quantity=qty,
        reference_task=None,
        reference_order_item=item,
        reference_color_part=issue.color_part,
        created_by=issued_by,
        fulfilled_issue=issue,
        note=note,
    )


@transaction.atomic
def execute_handover(*, issued_by, items, received_by=None, note='', held_by=None):
    """
    items: list[(issue_id, Decimal quantity)]
    held_by: اگر داده شود، باقی‌ماندهٔ بازِ بسته‌ها به‌جای «سالن تولید»
            در MaterialCustody همان کارگر نگه داشته می‌شود (مدل امانت نقاش).
            اگر داده نشود، رفتار قدیمی (MaterialLeftover سالن‌محور) اجرا می‌شود.
    همهٔ کارها در یک تراکنش انجام می‌شود؛ اگر هر مرحله خطا بدهد هیچ چیزی ثبت نمی‌شود.
    """
    if not items:
        raise HandoverError('هیچ موردی انتخاب نشده است.')

    issues_qty = _load_issues(items, lock=True)

    # قفل ردیف باقی‌مانده هر ماده هم‌زمان نقش mutex دارد: دو انباردار هم‌زمان
    # نمی‌توانند برای یک ماده موجودی را دوبار مصرف کنند.
    raw_ids = sorted({issue.raw_material_id for issue, _ in issues_qty})
    if held_by is not None:
        pool_rows = _lock_custody_rows(raw_ids, held_by)
    else:
        for raw_id in raw_ids:
            MaterialLeftover.objects.get_or_create(raw_material_id=raw_id)
        pool_rows = {
            row.raw_material_id: row
            for row in MaterialLeftover.objects.select_for_update()
            .filter(raw_material_id__in=raw_ids).order_by('raw_material_id')
        }

    plans = build_plans(
        issues_qty, {rid: row.quantity for rid, row in pool_rows.items()}, held_by=held_by
    )

    short = [p for p in plans if not p['enough']]
    if short:
        details = '؛ '.join(
            f"{p['raw'].name} (نیاز فیزیکی {p['physical']}، موجودی {p['stock']})" for p in short
        )
        raise HandoverError(f'موجودی انبار کافی نیست: {details}')

    handover = MaterialHandover.objects.create(
        issued_by=issued_by, received_by=held_by or received_by, note=(note or '')[:255],
    )

    for plan in plans:
        MaterialHandoverLine.objects.create(
            handover=handover,
            raw_material=plan['raw'],
            required_quantity=plan['need'],
            leftover_used=plan['from_leftover'],
            from_stock_quantity=plan['physical'],
            pack_size=plan['pack_size'],
            packs_count=plan['packs'],
            leftover_before=plan['leftover'],
            leftover_after=plan['new_leftover'],
        )

    now = timezone.now()
    for issue, qty in issues_qty:
        movement = _create_issue_movement(issue, qty, issued_by, handover)
        issue.issued_quantity += qty
        issue.status = 'issued' if issue.issued_quantity >= issue.requested_quantity else 'partial'
        issue.issued_by = issued_by
        receiver = held_by or received_by
        if receiver is not None:
            issue.received_by = receiver
        issue.issued_at = now
        issue.handover = handover
        issue.save(update_fields=[
            'issued_quantity', 'status', 'issued_by', 'received_by',
            'issued_at', 'handover',
        ])
        if issue.defect_id and issue.status == 'issued':
            issue.defect.status = 'rework_issued'
            issue.defect.save(update_fields=['status'])

    pool_label = f'نزد «{held_by.get_full_name() or held_by.username}»' if held_by else 'سالن تولید'
    for plan in plans:
        delta = plan['new_leftover'] - plan['leftover']
        if delta > 0:
            StockMovement.objects.create(
                raw_material=plan['raw'], movement_type='consumption', quantity=delta,
                created_by=issued_by,
                note=f'باقی‌ماندهٔ بسته/قوطی {pool_label} — تحویل شماره {handover.id}',
            )
        elif delta < 0:
            StockMovement.objects.create(
                raw_material=plan['raw'], movement_type='adjustment', quantity=-delta,
                created_by=issued_by,
                note=f'مصرف از باقی‌ماندهٔ {pool_label} (قبلاً از انبار کسر شده) — تحویل شماره {handover.id}',
            )
        row = pool_rows[plan['raw_material_id']]
        row.quantity = plan['new_leftover']
        row.save(update_fields=['quantity', 'updated_at'])

    return handover


# ---------------------------------------------------------------------------
# بازگشت پایان روز: ثبت مقدار واقعی توزین‌شده
# ---------------------------------------------------------------------------

@transaction.atomic
def return_custody(
    *, raw_material, held_by, measured_quantity, recorded_by, note='',
    to_warehouse=None, attributions=(),
):
    """
    انباردار در پایان روز وضعیت امانت هر کارگر را ثبت می‌کند.

    ``measured_quantity``
        مقدار توزین‌شده؛ **جایگزین** مقدار قبلی امانت می‌شود، نه جمع آن.
        یعنی مقداری که **همین حالا** دست کارگر است.
    ``to_warehouse``
        اگر کارگر بخشی از همان مقدار توزین‌شده را فیزیکاً به انبار برگرداند،
        موجودی انبار به همان اندازه زیاد می‌شود (حرکت `return`). نمی‌تواند از
        ``measured_quantity`` بیشتر باشد.
    ``attributions``
        فهرست ``(defect, quantity)``: مصرف این ماده برای کدام خرابی‌ها بوده است.

    حسابداری — چرا مصرف حرکت انبار نمی‌سازد:
        کل بستهٔ بازشده در لحظهٔ تحویل از انبار کسر شده و ``current_stock`` آن را
        «خارج‌شده» نشان می‌دهد. اگر اینجا دوباره حرکت مصرف ساخته شود، موجودی
        **دو بار** کسر می‌شود. پس انتساب مصرف فقط در جدول CustodyConsumption
        ثبت می‌شود و روی موجودی اثری ندارد.

    وضعیت‌های خاص که حرکت انبار می‌سازند:
        * ``measured < before``  → کارگر کمتر از ثبت‌شده دارد؛ آن مقدار به
          حساب دفتر امانت می‌آید ولی حرکت انبار نمی‌سازد (چیزی وارد انبار نشده).
        * ``measured > before``  → کارگر بیش از ثبت‌شده دارد؛ کسری انبار با
          حرکت ``adjustment`` ثبت می‌شود (انبار کم می‌شود).
    """
    if held_by is None:
        raise HandoverError('کارگر تحویل‌گیرنده مشخص نشده است.')
    if measured_quantity is None:
        raise HandoverError('مقدار توزین‌شده را وارد کنید.')
    try:
        measured = q2(measured_quantity)
    except (TypeError, ValueError, ArithmeticError, InvalidOperation):
        raise HandoverError('مقدار توزین‌شده نامعتبر است.')
    if not measured.is_finite() or measured < ZERO:
        raise HandoverError('مقدار توزین‌شده نمی‌تواند منفی باشد.')

    back = ZERO if to_warehouse in (None, '') else None
    if back is None:
        try:
            back = q2(to_warehouse)
        except (TypeError, ValueError, ArithmeticError, InvalidOperation):
            raise HandoverError('مقدار بازگشتی به انبار نامعتبر است.')
    if not back.is_finite() or back < ZERO:
        raise HandoverError('مقدار بازگشتی به انبار نمی‌تواند منفی باشد.')
    if back > measured:
        raise HandoverError(
            'مقدار بازگشتی به انبار نمی‌تواند از مقدار توزین‌شده بیشتر باشد '
            f'({back} > {measured}).'
        )

    custody, _ = MaterialCustody.objects.select_for_update().get_or_create(
        raw_material=raw_material, held_by=held_by, defaults={'quantity': ZERO}
    )
    before = q2(custody.quantity)

    # مصرف واقعی = امانت قبلی منهای آنچه هنوز دست کارگر است.
    # «بازگشت به انبار» جدا از این است و فقط موجودی انبار را زیاد می‌کند؛
    # آن مقدار از قبل در «توزین‌شده» دیده شده و در امانت باقی می‌ماند.
    consumed = q2(before - measured)

    custody.quantity = measured
    custody.save(update_fields=['quantity', 'updated_at'])

    if back > ZERO:
        StockMovement.objects.create(
            raw_material=raw_material,
            movement_type='return',
            quantity=back,
            created_by=recorded_by,
            note=f'بازگشت فیزیکی به انبار از امانت «{held_by}»'
                 + (f' — {note[:120]}' if note else ''),
        )
    elif consumed < ZERO:
        # کارگر بیش از مقدار ثبت‌شده در اختیار دارد؛ یعنی آن مقدار هیچ‌وقت از
        # موجودی انبار کسر نشده بود (تحویل ثبت‌نشده). پس موجودی را به همان
        # اندازه به انبار برمی‌گردانیم تا عدد انبار با واقعیت بخواند.
        StockMovement.objects.create(
            raw_material=raw_material,
            movement_type='adjustment',
            quantity=-consumed,
            created_by=recorded_by,
            note=f'کسری امانت «{held_by}» — بیش از مقدار ثبت‌شده در اختیار داشت'
                 + (f' — {note[:120]}' if note else ''),
        )

    record = MaterialCustodyReturn.objects.create(
        custody=custody,
        raw_material=raw_material,
        held_by=held_by,
        measured_quantity=measured,
        quantity_before=before,
        delta=q2(measured - before),
        warehouse_return=back,
        note=(note or '')[:255],
        recorded_by=recorded_by,
    )

    rows, total = _create_consumptions(
        record, raw_material=raw_material, held_by=held_by,
        attributions=attributions, available=consumed,
    )

    return {
        'record': record,
        'before': before,
        'measured': measured,
        'delta': record.delta,
        'consumed': consumed,
        'warehouse_return': back,
        'attributions': rows,
        'attributed_total': total,
        'shortfall': q2(consumed - total) if consumed > ZERO else ZERO,
    }


def _create_consumptions(record, *, raw_material, held_by, attributions, available):
    """
    انتساب مصرف به خرابی‌ها را می‌سازد و جمع آن را برمی‌گرداند.

    جمع انتساب‌ها نباید از مصرف واقعی بیشتر شود؛ اگر بیشتر باشد خطا می‌دهیم تا
    انباردار مبالغ را اصلاح کند. (کمتر بودن مجاز است: بخشی از مصرف ممکن است
    برای کار عادی باشد و خرابی ثبت نشده باشد.)
    """
    seen = set()
    rows = []
    total = ZERO
    for defect, qty in attributions:
        if defect is None:
            raise HandoverError('یکی از خرابی‌های انتخاب‌شده نامعتبر است.')
        if defect.pk in seen:
            raise HandoverError(f'خرابی شماره {defect.pk} بیش از یک‌بار انتخاب شده است.')
        seen.add(defect.pk)
        amount = q2(qty)
        if not amount.is_finite() or amount <= ZERO:
            raise HandoverError(f'مقدار مصرف برای خرابی شماره {defect.pk} باید بزرگ‌تر از صفر باشد.')
        total = q2(total + amount)
        if total > available:
            raise HandoverError(
                f'جمع مصرف ثبت‌شده ({total}) از مصرف واقعی ({available}) بیشتر است.'
            )
        rows.append(CustodyConsumption.objects.create(
            custody_return=record,
            raw_material=raw_material,
            held_by=held_by,
            defect=defect,
            quantity=amount,
        ))
    return rows, total


# ---------------------------------------------------------------------------
# گزارش خواندنی برای صفحهٔ «تحویل روزانه نقاشی»
# ---------------------------------------------------------------------------

def custody_overview(held_by=None, search='', only_open=False):
    """
    فقط خواندنی. خروجی: dict با کلیدهای rows و summary.

    rows: هر کدام یک دیکشنری شامل
        user, raw, unit, quantity, pack_size, updated_at, days_open
    summary: تعداد کارگران دارای امانت باز و مجموع مقدار امانت‌ها.
    """
    qs = (
        MaterialCustody.objects
        .select_related('raw_material', 'raw_material__category', 'held_by')
        .all()
    )
    if held_by is not None:
        qs = qs.filter(held_by=held_by)
    if search:
        qs = qs.filter(
            Q(raw_material__name__icontains=search)
            | Q(raw_material__code__icontains=search)
            | Q(held_by__username__icontains=search)
            | Q(held_by__first_name__icontains=search)
            | Q(held_by__last_name__icontains=search)
        )
    if only_open:
        qs = qs.filter(quantity__gt=0)

    now = timezone.now()
    rows = []
    for row in qs.order_by('-quantity', 'held_by__username', 'raw_material__name'):
        age = now - row.updated_at
        rows.append({
            'custody': row,
            'user': row.held_by,
            'raw': row.raw_material,
            'unit': row.raw_material.get_unit_display(),
            'quantity': row.quantity,
            'pack_size': row.raw_material.pack_size or ZERO,
            'updated_at': row.updated_at,
            'days_open': age.days,
        })

    open_qs = MaterialCustody.objects.filter(quantity__gt=0)
    if held_by is not None:
        open_qs = open_qs.filter(held_by=held_by)
    summary = {
        'open_rows': open_qs.count(),
        'workers_count': open_qs.values('held_by').distinct().count(),
        'total_quantity': open_qs.aggregate(total=Sum('quantity'))['total'] or ZERO,
    }
    return {'rows': rows, 'summary': summary}


# ---------------------------------------------------------------------------
# جمع مواد نیاز تحویل (نمای پیش از جدولِ ردیف‌های ریز)
# ---------------------------------------------------------------------------

def aggregate_needs(issues_qs, *, held_by=None):
    """
    مجموع نیاز مواد برای تحویل، گروه‌بندی‌شده بر اساس ماده اولیه.

    هر ردیز = یک ماده اولیه با جمع «باقی‌ماندهٔ تحویل‌نشده» آن درخواست‌ها.
    ورودی همان queryset صف تحویل است (قبل از صفحه‌بندی)، تا مجموع کل صف را
    نشان دهد نه فقط ۲۵ ردیف نخستِ نمایش‌داده‌شده.

    کنار هر ردیف، شناسهٔ درخواست‌های زیرمجموعه هم برمی‌گردد تا بتوان همان مقدار
    را با ``distribute_aggregate`` بین آن‌ها پخش کرد.
    """
    rows = issues_qs.values_list('id', 'raw_material_id', 'raw_material__name',
                                 'requested_quantity', 'issued_quantity')

    buckets = {}
    for issue_id, raw_id, raw_name, requested, issued in rows:
        remaining = q2(q2(requested or ZERO) - q2(issued or ZERO))
        if remaining <= ZERO:
            continue
        entry = buckets.get(raw_id)
        if entry is None:
            entry = buckets[raw_id] = {
                'raw_material_id': raw_id,
                'name': raw_name,
                'need': ZERO,
                'issue_ids': [],
            }
        entry['need'] = q2(entry['need'] + remaining)
        entry['issue_ids'].append(issue_id)

    result = []
    for raw_id in sorted(buckets, key=lambda k: (buckets[k]['name'] or '', k)):
        entry = buckets[raw_id]
        raw = RawMaterial.objects.get(pk=raw_id)
        result.append({
            'raw_material_id': raw_id,
            'raw': raw,
            'name': raw.name,
            'unit': raw.get_unit_display(),
            'need': entry['need'],
            'pack_size': q2(raw.pack_size or ZERO),
            'stock': q2(raw.current_stock),
            'issue_ids': entry['issue_ids'],
            'issue_count': len(entry['issue_ids']),
        })
    return result


def distribute_aggregate(raw_material_id, quantity):
    """
    یک مقدار کل را **تناسبی** بین درخواست‌های بازِ یک ماده پخش می‌کند.

    خروجی: ``list[(issue_id, Decimal)]`` — همان فرمتی که ``execute_handover``
    می‌گیرد. سهم هر درخواست برابر نسبتِ نیاز باقی‌ماندهٔ او به جمع کل است، و
    باقی‌ماندهٔ گِردکردن به آخرین درخواست داده می‌شود تا جمع دقیقاً برابر مقدار
    ورودی بماند.

    مقدار ورودی نباید از مجموع نیاز باقی‌مانده بیشتر باشد.
    """
    if quantity is None:
        raise HandoverError('مقدار تحویل را وارد کنید.')
    try:
        total = q2(quantity)
    except (TypeError, ValueError, ArithmeticError, InvalidOperation):
        raise HandoverError('مقدار تحویل نامعتبر است.')
    if not total.is_finite() or total <= ZERO:
        raise HandoverError('مقدار تحویل باید بزرگ‌تر از صفر باشد.')

    pending = list(
        MaterialIssue.objects
        .filter(raw_material_id=raw_material_id, status__in=['requested', 'partial'])
        .order_by('created_at', 'pk')
    )
    remainings = [q2(i.requested_quantity - i.issued_quantity) for i in pending]
    pairs = [(i, r) for i, r in zip(pending, remainings) if r > ZERO]
    if not pairs:
        raise HandoverError('برای این ماده درخواست بازی وجود ندارد.')

    total_need = q2(sum((r for _, r in pairs), ZERO))
    if total > total_need:
        raise HandoverError(
            f'مقدار تحویل ({total}) از مجموع نیاز باقی‌مانده ({total_need}) بیشتر است.'
        )

    if total == total_need:
        return [(i.pk, r) for i, r in pairs]

    items, allocated = [], ZERO
    last = len(pairs) - 1
    for idx, (issue, remaining) in enumerate(pairs):
        if idx == last:
            share = q2(total - allocated)
        else:
            share = min(q2(total * remaining / total_need), q2(total - allocated))
        if share <= ZERO:
            continue
        items.append((issue.pk, share))
        allocated = q2(allocated + share)
    return items


def open_defect_choices(*, search='', raw_material=None, limit=50):
    """
    فهرست خرابی‌های باز برای انتساب مصرف در پایان روز.

    خرابی‌هایی که هنوز مواد جایگزینشان صادر نشده‌اند. اگر ``raw_material`` داده
    شود، فقط خرابی‌هایی می‌آیند که قبلاً برای همین ماده درخواست/تحویل داشته‌اند،
    تا انتخاب‌ها به کار واقعی نزدیک بماند.
    """
    from product.models import ProductionDefect

    qs = ProductionDefect.objects.filter(
        status__in=['reported', 'material_requested']
    ).select_related('order', 'order_item__product', 'packaging_unit')

    if raw_material is not None:
        qs = qs.filter(
            Q(material_issues__raw_material=raw_material) |
            Q(order_item__material_issues__raw_material=raw_material) |
            Q(packaging_unit__order_item__material_issues__raw_material=raw_material)
        ).distinct()
    if search:
        qs = qs.filter(
            Q(order__id__icontains=search) |
            Q(description__icontains=search) |
            Q(packaging_unit__unit_number__icontains=search) |
            Q(order_item__product__name__icontains=search)
        )
    return list(qs.order_by('-created_at')[:limit])


# ---------------------------------------------------------------------------
# Phase 2: Daily Material Queue (standalone from legacy custody)
# ---------------------------------------------------------------------------

from decimal import Decimal, ROUND_CEILING as _RCEIL
import logging as _logging
_logger = _logging.getLogger(__name__)


def _q2(value):
    """Round to 2 decimal places."""
    if value is None:
        return Decimal('0.00')
    return Decimal(str(value)).quantize(Decimal('0.01'))


def _packs_counted(need, pack_size):
    """
    تعداد بستهٔ کامل لازم برای پوشش *need* — تنها قانون سقف بسته‌بندی در این ماژول.

    عمداً با ``Decimal`` و ``ROUND_CEILING`` محاسبه می‌شود، نه با ``float``:
    در محاسبهٔ اعشاری، ``0.07 / 0.01`` برابر ``7.000000000000001`` درمی‌آید و
    سقف آن ۸ می‌شود؛ یعنی برای نیاز ۰٫۰۷ کیلو یک بستهٔ کامل اضافه تحویل
    می‌گردد. این همان قانونی است که مسیر قدیمی تحویل مواد
    (``ROUND_CEILING``) استفاده می‌کند.
    """
    need = _q2(need)
    pack = _q2(pack_size or 0)
    if need <= 0 or pack <= 0:
        return 0
    return int((need / pack).to_integral_value(rounding=ROUND_CEILING))


def _physical_for(need, pack_size):
    """مقدار فیزیکی تحویل‌شده با گرد کردن رو به بالا به بستهٔ کامل."""
    need = _q2(need)
    pack = _q2(pack_size or 0)
    if need <= 0 or pack <= 0:
        return 0, need
    count = _packs_counted(need, pack)
    return count, _q2(pack * count)


def _packs_for(need, pack_size):
    """Return number of full packages needed to cover *need*."""
    count, physical = _physical_for(need, pack_size)
    return count, physical


def _resolve_painting_requirements_for_task(task):
    """
    تنها مسیر صف روزانه به نیاز نقاشی.

    Returns ``(resolved, errors, context)`` از همان resolver canonical در
    ``product.utils``؛ resolved هر عضوش dict با کلیدهای ``requirement`` و
    ``raw_material`` (ماده واقعی) است.
    """
    from product.utils import get_resolved_painting_requirements_for_task
    return get_resolved_painting_requirements_for_task(task)


def _work_date(value):
    """Convert a (possibly tz-aware) datetime/date into the local work date."""
    if value is None:
        return None
    from django.utils import timezone
    if hasattr(value, 'time'):
        try:
            return timezone.localtime(value).date()
        except Exception:
            return value.date()
    return value


def _task_date(task):
    return _work_date(task.scheduled_start)


# ---------------------------------------------------------------------------
# Eligibility of a ProductionTask for the daily delivery queue.
#
# Phase 3 / Decision 3: a task enters the daily queue ONLY when it has a
# scheduled date, an explicitly assigned worker and a painting stage.
# A task without a worker never becomes a warehouse delivery row.
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
}


def task_queue_ineligibility(task):
    """Return None if the task may feed the daily queue, else a skip reason key."""
    if task.station_name != 'paint':
        return 'not_painting'
    if _task_date(task) is None:
        return 'no_schedule'
    if not task.assigned_worker_id:
        return 'no_worker'
    if not task.painting_stage_id:
        return 'no_stage'
    if not task.order_item_id or not task.order_item.product_id:
        return 'no_order_item'
    if not task.color_part:
        return 'no_color_part'
    if not task.quantity or int(task.quantity) <= 0:
        return 'no_quantity'
    return None


def _painting_work_unit_key(task, raw_material_id):
    """
    کلید «واحد کار نقاشی» — مبنای de-duplication نیاز مواد.

    ``PaintingMaterialRequirement`` در سطح (process + product + color_part)
    تعریف شده و هیچ FK به ``PaintingStage`` ندارد. یک ``OrderItem`` برای هر
    مرحله یک ``ProductionTask`` جداگانه دارد، بنابراین اگر فرمول برای هر task
    اعمال شود به تعداد مراحل ضرب می‌شود: ۶ مرحله × ۱٫۳ = ۷٫۸ به‌جای ۲٫۶.

    واحد کار نقاشی = (order_item, color_part, painting_process, raw_material).

    de-duplication داخل هر گروه (worker, raw_material) انجام می‌شود؛ بنابراین
    دو کارگر مستقل که در یک روز روی دو واحد کار متفاوت کار می‌کنند هر کدام
    نیاز خودشان را نگه می‌دارند، و همان واحد کار در روز دیگر دوباره شمرده
    می‌شود چون صف هر روز مستقل است.
    """
    return (
        task.order_item_id,
        task.color_part or '',
        task.painting_stage.process_id,
        raw_material_id,
    )


def aggregate_queue_requirements(tasks):
    """
    Aggregate planned material needs for *tasks* (painting tasks of one day).

    Returns ``(grouped, diagnostics)``:
        grouped:      OrderedDict keyed by (worker_id, raw_material_id)
        diagnostics:  {'skipped': [...], 'multistage_overlaps': [...]}

    Phase 3 rules applied here:
        * planned quantity comes from ProductionTask.quantity (the production
          quantity of that task) — never from completed_quantity, so a
          half-finished task keeps its full planned need (Decision 10);
        * the worker is always the explicitly assigned one (Decision 3);
        * a process-level requirement is counted once per painting work unit
          (see ``_painting_work_unit_key``) and not once per PaintingStage. When
          several tasks share one work unit, the largest task quantity defines
          the unit size and the remaining tasks are kept as traceable sources
          with zero share, so ``sum(source.quantity) == planned_quantity``.
    """
    from collections import OrderedDict
    grouped = OrderedDict()
    diagnostics = {'skipped': [], 'multistage_overlaps': [],
                   'unresolved_materials': [], 'mapping_notes': []}

    for task in tasks:
        reason = task_queue_ineligibility(task)
        if reason is not None:
            if reason != 'not_painting':
                diagnostics['skipped'].append(
                    {'task_id': task.pk, 'reason': reason,
                     'label': _SKIP_REASONS.get(reason, reason)}
                )
            continue

        resolved, resolution_errors, context = _resolve_painting_requirements_for_task(task)
        if resolution_errors:
            # نیاز وجود دارد ولی ماده واقعی‌اش قابل تعیین نیست؛ هیچ صفی با ماده
            # حدسی ساخته نمی‌شود و علت دقیح گزارش می‌شود — حتی وقتی نیازهای
            # دیگر همان تسک درست resolve شده باشند.
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
            continue

        worker = task.assigned_worker
        stage = task.painting_stage
        try:
            task_qty = Decimal(str(task.quantity))
        except (TypeError, ValueError):
            task_qty = Decimal('0')

        for resolved_entry in resolved:
            requirement = resolved_entry['requirement']
            raw = resolved_entry['raw_material']
            note = resolved_entry.get('note')
            if note is not None:
                diagnostics['mapping_notes'].append(note.as_dict())
            try:
                consumption = Decimal(str(requirement.consumption_per_unit))
            except (TypeError, ValueError):
                consumption = Decimal('0')
            # جمع با دقت کامل انجام می‌شود و فقط یک‌بار هنگام نوشتن در دیتابیس
            # گرد می‌شود. گرد کردن هر منبع به‌صورت جداگانه باعث می‌شد نیازهای
            # کوچک (مثل 0.004) بی‌صدا حذف شوند و مجموع کمتر از واقع شود.
            qty = task_qty * consumption
            if qty <= 0:
                continue

            key = (worker.pk, raw.pk)
            entry = grouped.get(key)
            if entry is None:
                entry = grouped[key] = {
                    'worker': worker,
                    'raw_material': raw,
                    'quantity': Decimal('0'),
                    'sources': [],
                    'stages_by_process': {},
                    'work_units': {},
                }

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
                # بزرگ‌ترین مقدار تولید، اندازهٔ واحد کار را تعیین می‌کند.
                # representative جابه‌جا می‌شود و سهم قبلی به صفر می‌رسد تا
                # جمع منابع با مقدار صف برابر بماند.
                unit['shared'].append(
                    (unit['representative'][0], unit['representative'][1]))
                unit['representative'] = (task, stage, raw, qty)
                unit['quantity'] = qty
            else:
                unit['shared'].append((task, stage))

    # جمع نهایی از واحدهای کاری یکتا ساخته می‌شود، نه از تعداد taskها.
    for entry in grouped.values():
        for unit in entry.pop('work_units').values():
            entry['quantity'] += unit['quantity']
            representative = unit['representative']
            entry['sources'].append(representative)
            for shared_task, shared_stage in unit['shared']:
                # ردیف منبع بدون سهم مستقل: فقط نشان می‌دهد این مرحله در همان
                # واحد کار نقاشی شریک است (traceability بدون افزودن به جمع).
                entry['sources'].append(
                    (shared_task, shared_stage, unit['raw_material'], Decimal('0')))
            stages = entry['stages_by_process'].setdefault(unit['process_id'], {})
            stage_id = representative[1].id
            stages[stage_id] = stages.get(stage_id, Decimal('0')) + unit['quantity']

    # Decision 2 (report only): چند واحد کار نقاشی از یک process که در یک روز و
    # برای یک کارگر/ماده به صف رسیده‌اند گزارش می‌شوند. این دیگر بیش‌برنامه‌ریزی
    # نیست؛ هر واحد کار جداگانه شمرده شده است.
    for (worker_id, raw_id), entry in grouped.items():
        for process_id, stages in entry['stages_by_process'].items():
            if len(stages) > 1:
                diagnostics['multistage_overlaps'].append({
                    'worker_id': worker_id,
                    'raw_material_id': raw_id,
                    'process_id': process_id,
                    'stage_ids': sorted(stages.keys()),
                    'total_quantity': _q2(sum(stages.values())),
                })

    return grouped, diagnostics


def detect_multistage_overlaps(date):
    """
    Report-only check: painting work units of the same process that reach the
    same worker/material on *date* through different stages. No database
    writes, no model changes.

    Since the requirement is counted once per painting work unit, this is a
    planning observation (several colour parts/orders of one process reaching
    one worker on one day), not a quantity inflation.
    """
    _tasks = _get_scheduled_painting_tasks(date)
    _grouped, diagnostics = aggregate_queue_requirements(_tasks)
    return diagnostics['multistage_overlaps']


def log_queue_diagnostics(date, diagnostics):
    """Surface aggregation diagnostics (skipped tasks, stage overlaps)."""
    overlaps = diagnostics.get('multistage_overlaps') or []
    for item in overlaps:
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
        # نقص دادهٔ کاتالوگ: نیاز به مادهٔ FK خودش حل شد، پس این فقط گزارش است
        # و عمداً در سطح info می‌ماند تا نویز عملیاتی نسازد.
        _logger.info(
            'sync_daily_material_queue: %s — نیاز نقاشی #%s (process=%s, '
            'material=%s): %s',
            date, item.get('requirement_id'), item.get('process_id'),
            item.get('slot_raw_material_id'), item.get('label'),
        )
    seen_unresolved = set()
    for item in diagnostics.get('unresolved_materials') or []:
        unresolved_key = (item.get('requirement_id'), item.get('process_id'),
                          item.get('slot_raw_material_id'), item.get('color_code'))
        if unresolved_key in seen_unresolved:
            continue
        seen_unresolved.add(unresolved_key)
        # نیاز نقاشی به ماده اولیه واقعی نگاشت نشد؛ عمداً هیچ صفی با ماده
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
    return overlaps


def _get_scheduled_painting_tasks(date):
    from product.models import ProductionTask
    from django.utils import timezone
    from datetime import datetime, timedelta
    day_start = datetime.combine(date, datetime.min.time())
    if timezone.is_aware(day_start):
        start = day_start
    else:
        start = timezone.make_aware(day_start)
    end = start + timedelta(days=1)
    qs = (
        ProductionTask.objects
        .filter(station_name='paint', scheduled_start__gte=start, scheduled_start__lt=end)
        .select_related('painting_stage', 'order_item', 'order_item__product',
                        'assigned_worker', 'scanned_by')
    )
    return list(qs)


CONFLICT_NOTE_PLAN_CHANGED = (
    'تغییر برنامه پس از ثبت تحویل/برگشت اعمال نشد؛ تاریخچهٔ واقعی حفظ شد.'
)
CONFLICT_NOTE_DROPPED = (
    'نیاز از برنامهٔ نقاشی حذف شد ولی تحویل/برگشت ثبت‌شده دارد؛ '
    'موجودی و تاریخچه دست‌نخورده ماند.'
)


def _mark_conflict(queue, note):
    """Flag a plan/transaction conflict without touching real movement history."""
    fields = ['has_plan_conflict', 'conflict_note', 'updated_at']
    queue.has_plan_conflict = True
    queue.conflict_note = (note or '')[:255]
    queue.save(update_fields=fields)


def build_daily_queue_for_date(date):
    """
    Build (or refresh) DailyMaterialQueue rows for *date*.

    Derives the day's needs from the painting schedule
    (ProductionTask -> PaintingStage -> PaintingMaterialRequirement), grouped
    by (worker, raw_material), with full traceability in
    DailyMaterialQueueSource.

    Decision 6 (transaction guard):
        * a queue with no delivery/return is fully re-synchronised;
        * a queue that already has ``delivered_quantity > 0`` or
          ``returned_quantity > 0`` keeps its planned quantity and its sources,
          and the plan change is only flagged through ``has_plan_conflict`` /
          ``conflict_note``. Real stock movements are never rewritten.

    Idempotent: running it repeatedly never duplicates rows or sources.
    """
    from .models import DailyMaterialQueue, DailyMaterialQueueSource
    from django.db import transaction

    tasks = _get_scheduled_painting_tasks(date)
    grouped, diagnostics = aggregate_queue_requirements(tasks)

    with transaction.atomic():
        existing = list(
            DailyMaterialQueue.objects.select_for_update()
            .filter(work_date=date)
        )
        existing_keys = {(q.worker_id, q.raw_material_id): q for q in existing}

        for (worker_id, raw_id), entry in grouped.items():
            # تنها نقطهٔ گرد کردن: مقدار نهایی و قابل ذخیره در فیلد دومقطری.
            planned = _q2(entry['quantity'])
            queue = existing_keys.get((worker_id, raw_id))

            if queue is None:
                queue = DailyMaterialQueue(
                    work_date=date,
                    worker=entry['worker'],
                    raw_material=entry['raw_material'],
                    planned_quantity=planned,
                    status='pending',
                )
                queue.save()
            elif queue.has_transaction:
                # Never overwrite a delivered/returned row's plan history.
                if _q2(queue.planned_quantity) != planned:
                    _mark_conflict(queue, CONFLICT_NOTE_PLAN_CHANGED)
                continue
            else:
                queue.planned_quantity = planned
                queue.worker = entry['worker']
                queue.raw_material = entry['raw_material']
                if queue.status == 'cancelled':
                    # The need is back in the schedule: revive the row.
                    queue.status = 'pending'
                queue.has_plan_conflict = False
                queue.conflict_note = ''
                queue.save()

            # Sources are refreshed only for rows without real movements.
            DailyMaterialQueueSource.objects.filter(queue=queue).delete()
            for task, stage, raw, qty in entry['sources']:
                DailyMaterialQueueSource.objects.create(
                    queue=queue,
                    production_task=task,
                    painting_stage=stage,
                    raw_material=raw,
                    quantity=_q2(qty),
                )

        # Rows that are no longer part of the schedule.
        active_keys = set(grouped.keys())
        for key, queue in existing_keys.items():
            if key in active_keys:
                continue
            if queue.has_transaction:
                # Delivery/return history exists: keep it, flag the conflict.
                if not queue.has_plan_conflict:
                    _mark_conflict(queue, CONFLICT_NOTE_DROPPED)
                continue
            # بدون تراکنش واقعی، منابع برنامه‌ریزی هم به روز/کارگر قبلی تعلق دارند
            # و باید پاک شوند؛ در غیر این صورت یک تسک که جابه‌جا شده هم در ردیف
            # لغوشده و هم در ردیف جدید منبع می‌ماند و در ردیابیِ موادِ سفارش دوبار
            # شمرده می‌شود. ردیف لغوشده قابل تحویل نیست، پس چیزی از تاریخچهٔ
            # واقعی از دست نمی‌رود.
            if queue.sources.exists():
                queue.sources.all().delete()
            if queue.status != 'cancelled':
                queue.status = 'cancelled'
                queue.save(update_fields=['status', 'updated_at'])

    log_queue_diagnostics(date, diagnostics)

    return list(
        DailyMaterialQueue.objects
        .filter(work_date=date)
        .order_by('worker', 'raw_material__name')
    )


def sync_daily_material_queue(date):
    """
    Central sync entry point. Rebuilds the daily queue for *date*
    from the Painting Schedule (ProductionTask -> PaintingStage ->
    PaintingMaterialRequirement). Idempotent: safe to call multiple times.

    Returns the list of DailyMaterialQueue rows.
    """
    return build_daily_queue_for_date(date)


def sync_daily_material_queue_for_dates(dates):
    """
    Sync several work dates inside ONE transaction (Decision 5: moving a task
    from day 1 to day 2 must remove the need from day 1 and create/update it on
    day 2 atomically).

    Raises on failure so the caller can roll the whole schedule change back.
    """
    from django.db import transaction

    cleaned = []
    for d in dates or ():
        if d is None:
            continue
        cleaned.append(_work_date(d))
    unique_dates = sorted(set(cleaned))

    results = []
    if not unique_dates:
        return results
    with transaction.atomic():
        for d in unique_dates:
            results.extend(sync_daily_material_queue(d))
    return results


def _affected_dates_for_tasks(task_ids):
    """Return set of Gregorian work dates affected by the given task IDs."""
    if not task_ids:
        return set()

    from product.models import ProductionTask

    dates = set()
    for start in (
        ProductionTask.objects.filter(pk__in=task_ids)
        .values_list('scheduled_start', flat=True)
    ):
        d = _work_date(start)
        if d is not None:
            dates.add(d)
    return dates


# ---------------------------------------------------------------------------
# Batching of sync requests
#
# Bulk scheduling touches hundreds of tasks at once. Every one of them asks for
# a queue sync; instead of rebuilding one day per task, the callers open
# ``queue_sync_scope()`` and all requests collected inside it are applied once,
# in one transaction, when the scope closes. If the scope raises, the pending
# dates are dropped together with the rolled-back schedule change.
# ---------------------------------------------------------------------------

_queue_sync_state = threading.local()


def _pending_sync_dates():
    return getattr(_queue_sync_state, 'dates', None)


@contextlib.contextmanager
def queue_sync_scope():
    """Collect queue-sync dates raised inside the block and flush them once."""
    previous = getattr(_queue_sync_state, 'dates', None)
    _queue_sync_state.dates = set()
    try:
        yield _queue_sync_state.dates
    except Exception:
        _queue_sync_state.dates = previous
        raise
    pending = _queue_sync_state.dates
    _queue_sync_state.dates = previous
    if pending:
        try:
            sync_daily_material_queue_for_dates(pending)
        except Exception:
            _logger.exception('queue_sync_scope: failed to sync dates %s', sorted(pending))


def request_queue_sync(dates):
    """
    Ask for the daily queue of *dates* to be re-derived. Best effort: never
    raises, never breaks the caller's business logic. Inside a
    ``queue_sync_scope`` the dates are only collected.
    """
    cleaned = {_work_date(d) for d in (dates or ()) if d is not None}
    if not cleaned:
        return []

    pending = _pending_sync_dates()
    if pending is not None:
        pending |= cleaned
        return []

    try:
        return sync_daily_material_queue_for_dates(cleaned)
    except Exception:
        _logger.exception('request_queue_sync: failed to sync dates %s', sorted(cleaned))
        return []


def request_queue_sync_for_tasks(task_ids, extra_dates=()):
    """Request a queue sync for the dates of *task_ids* plus any *extra_dates*."""
    dates = set(_affected_dates_for_tasks(task_ids))
    dates |= {_work_date(d) for d in (extra_dates or ()) if d is not None}
    return request_queue_sync(dates)


def sync_queue_for_tasks(task_ids, *, extra_dates=()):
    """
    Rebuild DailyMaterialQueue for all dates affected by *task_ids*.
    Never raises; inside a ``queue_sync_scope`` it only collects the dates.
    """
    try:
        if not task_ids:
            return request_queue_sync(extra_dates)
        task_ids = list(set(int(t) for t in task_ids))
        return request_queue_sync_for_tasks(task_ids, extra_dates=extra_dates)
    except Exception:
        _logger.exception('sync_queue_for_tasks: error for tasks %s', task_ids)
        return []


def sync_queue_for_date(date):
    """Rebuild DailyMaterialQueue for a single date. Never raises."""
    try:
        return request_queue_sync([date])
    except Exception:
        _logger.exception('sync_queue_for_date: error syncing date %s', date)
        return []


def sync_queue_safe(task_ids=None, date=None):
    """Best-effort sync wrapper used by views. Never raises."""
    try:
        if date is not None:
            return sync_queue_for_date(date)
        if task_ids:
            return sync_queue_for_tasks(task_ids)
        return []
    except Exception:
        _logger.exception('sync_queue_safe: unexpected error')
        return []


@transaction.atomic
def execute_daily_delivery(*, queue_id, delivered_by, note='', items=None):
    """
    Execute delivery for one DailyMaterialQueue row.

    Computes the physical quantity to deliver using the packaging rule:
        physical = ceil(planned / pack_size) * pack_size  (if pack_size > 0)
        otherwise physical = planned

    Creates a StockMovement(consumption) for the physical quantity,
    updates delivered_quantity and status.

    This function is the single source of truth for the delivery quantity;
    callers (views/UI) must not re-implement the packaging rule.

    *items* (optional): list of (source_id, delivered_qty) for controlled
    partial delivery. If omitted, the full planned quantity is delivered.

    Idempotency / concurrency:
        The queue row is locked with ``select_for_update`` for the whole
        operation, so two simultaneous requests can never both consume stock.
        Any row that already has a real movement (``has_transaction``) is
        refused, which also covers statuses like ``returned`` where a
        naive status check would otherwise allow a second consumption.
    """
    from .models import DailyMaterialQueue, StockMovement

    try:
        queue = DailyMaterialQueue.objects.select_for_update().get(pk=queue_id)
    except DailyMaterialQueue.DoesNotExist:
        raise HandoverError('ردیف صف مواد روزانه یافت نشد.')

    if queue.has_transaction or queue.status in ('delivered', 'returned', 'closed'):
        raise HandoverError('این ردیف قبلاً تحویل داده شده است و تحویل دوبارهٔ آن ممکن نیست.')

    if queue.status == 'cancelled':
        raise HandoverError(
            'این ردیف از برنامهٔ روز حذف شده است؛ تا زمانی که نیاز آن در برنامه نباشد، تحویل ممکن نیست.'
        )

    planned = _q2(queue.planned_quantity)
    pack = _q2(queue.raw_material.pack_size or 0)

    if planned <= 0:
        raise HandoverError('مقدار برنامه‌ریزی‌شده برای این ردیف صفر است؛ تحویلی انجام نمی‌شود.')

    if items:
        total_delivered = sum(_q2(q) for _, q in items)
        if total_delivered <= 0:
            raise HandoverError('مقدار تحویل باید بزرگ‌تر از صفر باشد.')
    else:
        total_delivered = planned

    # Packaging rule: if need=3kg and pack_size=4kg, deliver 4kg
    packs, physical = _physical_for(total_delivered, pack)

    stock = _q2(queue.raw_material.current_stock)
    if physical > stock:
        raise HandoverError(
            f'موجودی انبار کافی نیست. برای «{queue.raw_material.name}» '
            f'مقدار تحویل {physical} لازم است اما موجودی {stock} است.'
        )

    # تا این‌جا هیچ نوشتنی انجام نشده؛ از این نقطه به بعد حرکت انبار و به‌روزرسانی
    # صف در همان تراکنش انجام می‌شود، پس خطا هیچ تغییری باقی نمی‌گذارد.
    StockMovement.objects.create(
        raw_material=queue.raw_material,
        movement_type='consumption',
        quantity=physical,
        created_by=delivered_by,
        note=(note or '')[:255] or f'تحویل روزانه — صف #{queue.id}',
    )
    # مقدار ثبت‌شده در صف باید دقیقاً همان چیزی باشد که از انبار کم شده است.
    # planned_quantity (نیاز برنامه‌ریزی‌شده) دست‌نخورده می‌ماند و ممکن است کمتر
    # از مقدار فیزیکی باشد؛ مابه‌التفاوت (پک اضافه) یا مصرف اضافه گزارش می‌شود یا
    # به‌صورت برگشتی برمی‌گردد. اگر اینجا مقدار نیاز ذخیره شود، موجودی انبار با صف
    # reconcile نمی‌شود و پک اضافه هرگز قابل برگشت نیست.
    queue.delivered_quantity = physical
    queue.status = 'delivered'
    queue.recalculate_consumption()
    # planned_quantity و منابع برنامه‌ریزی دست‌نخورده می‌مانند.
    queue.save(update_fields=[
        'delivered_quantity', 'status',
        'actual_consumption', 'excess_consumption', 'updated_at',
    ])

    return queue


@transaction.atomic
def execute_daily_return(*, queue_id, returned_by, returned_quantity, note=''):
    """
    Register a physical return of material to the warehouse for a
    DailyMaterialQueue row. Creates a real StockMovement(return), which
    increases warehouse stock. This is the NEW return path (Decision 5).

    Accounting rules enforced here:
        * returned total can never exceed delivered total, so
          ``actual_consumption = delivered - returned`` never goes negative;
        * a return never increases actual consumption, it only reduces it;
        * planned quantity and planning sources are never touched;
        * the returned material becomes ordinary warehouse stock — no
          MaterialCustody / reservation is created for the worker.

    Partial returns are supported: several returns may be recorded for the
    same queue as long as their sum does not exceed ``delivered_quantity``.
    Once nothing is left to return, further returns are refused (idempotency).

    The queue row is locked with ``select_for_update`` for the whole
    operation, so two simultaneous requests can never both create a
    StockMovement(return) for the same queue row.

    The legacy return_custody() is unchanged and still used for the
    old MaterialCustody workflow.
    """
    from .models import DailyMaterialQueue, StockMovement

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
            f'مقدار برگشتی ({ret}) نمی‌تواند از مقدار تحویل‌شدهٔ برگشت‌نشده ({max_returnable}) بیشتر باشد.'
        )

    # Decision 7: a returned material is ordinary warehouse stock again.
    # The movement goes straight to the general stock ledger; no custody /
    # reservation is created for the worker.
    StockMovement.objects.create(
        raw_material=queue.raw_material,
        movement_type='return',
        quantity=ret,
        created_by=returned_by,
        note=(note or '')[:255] or f'بازگشت روزانه — صف #{queue.id}',
    )
    queue.returned_quantity = _q2(already_returned + ret)
    if queue.returned_quantity >= delivered:
        queue.status = 'returned'
    else:
        queue.status = 'delivered'
    # planned_quantity و منابع برنامه‌ریزی دست‌نخورده می‌مانند.
    queue.recalculate_consumption()
    queue.save(update_fields=[
        'returned_quantity', 'status',
        'actual_consumption', 'excess_consumption', 'updated_at',
    ])

    return queue


def is_paint_need_delivered_by_queue(task, raw_material):
    """
    P7 — آیا نیاز نقاشی عادیِ این تسک برای این ماده از صف مواد
    روزانه **تحویل شده** است؟

    خواندنی‌است و هیچ نوشتنی انجام نمی‌دهد. پاسخ ``True`` فقط
    وقتی قطعی است، یعنی هر سه شرط همزمان برقرارند:

        * تسک، تسک نقاشی است (``station_name='paint'``)؛
        * مادهٔ مصرف دستی همان مادهٔ نیاز صف است؛
        * حداقل یک ``DailyMaterialQueue`` با ``delivered_quantity > 0``
          (تحویل واقعی — همان معنای ``has_transaction``) این تسک و
          این ماده را به‌عنوان source دارد.

    چرا همان ماده هم بخشی از شرط است:
        مصرف دستی برای ماده‌ای دیگر هرگز تکرار تحویل صف نیست.
        صرفاً وجود صف برای یک ماده در یک روز، مصرف دستی مستقل
        معتبر را باید بلاک نکند.

    چرا به‌جای property ``has_transaction`` روی فیلد فیلتر می‌کنیم:
        ``has_transaction`` یک property پایتونی است و در queryset
        قابل فیلتر نیست؛ معادل DB آن ``delivered_quantity > 0`` است
        (برگشتی بدون تحویل ممکن نیست).

    کاربرد:
        گارد مصرف دستی (W13) — اگر بستهٔ فیزیکی نیاز نقاشی قبلاً
        از انبار خارج شده است، مصرف دستی همان نیاز موجودی را دوبار
        کم می‌کند. هیچ ربطی به Engine A (MaterialIssue) ندارد و
        هیچ رفتاری در مسیر عملیاتی تغییر نمی‌کند.
    """
    from .models import DailyMaterialQueueSource

    if task is None or raw_material is None:
        return False
    try:
        if task.station_name != 'paint':
            return False
    except AttributeError:
        return False
    return DailyMaterialQueueSource.objects.filter(
        production_task=task,
        raw_material=raw_material,
        queue__delivered_quantity__gt=0,
    ).exists()


# ---------------------------------------------------------------------------
# گزارش مصرف مواد روزانه (Phase 7)
#
# این گزارش فقط «می‌خواند» و هیچ چیزی نمی‌نویسد. مصرف واقعی از فیلد
# ``actual_consumption`` خودِ ردیف صف خوانده می‌شود — همان فیلدی که
# ``recalculate_consumption`` از «تحویل منهای برگشت» نگه می‌دارد — تا هیچ
# منطق موازی یا تعریف دومی از مصرف ایجاد نشود.
#
# منبع حقیقت موجودی انبار همچنان ``StockMovement`` است؛ این گزارش به دفتر
# حرکات دست نمی‌زند و StockMovement جدیدی نمی‌سازد. برگشتی‌ها صرفاً
# موجودی عمومی انبار هستند و در این گزارش هرگز به‌عنوان مصرف شمرده نمی‌شوند.
# ---------------------------------------------------------------------------

def daily_queue_filtered_queryset(date, *, worker_id=None, material_id=None,
                                   status=None, include_cancelled=False):
    """
    queryset خام صف مواد روزانه برای *date* با فیلترهای اختیاری.

    از این متد در صفحهٔ انبار و هر گزارش دیگری استفاده می‌شود تا تعریف
    «یک ردیف صف» در یک نقطه بماند.
    """
    from .models import DailyMaterialQueue

    qs = DailyMaterialQueue.objects.filter(work_date=date)

    if worker_id:
        qs = qs.filter(worker_id=worker_id)
    if material_id:
        qs = qs.filter(raw_material_id=material_id)
    if status:
        qs = qs.filter(status=status)
    if not include_cancelled and status != 'cancelled':
        # ردیف لغوشده نیاز فعالی ندارد و در گزارش عملیات روزانه گمراه‌کننده است.
        # اما اگر کاربر صریحاً «لغو شده» را فیلتر کرده باشد، انتخاب او معتبر است.
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

    return {'rows': rows_qs, 'summary': summary}


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

    can_deliver = (
        not queue.has_transaction
        and queue.status not in ('delivered', 'returned', 'closed', 'cancelled')
        and planned > 0
    )
    can_return = (
        queue.status != 'cancelled'
        and delivered > 0
        and max_returnable > 0
    )

    return {
        'status': queue.status,
        'status_display': queue.get_status_display(),
        'can_deliver': can_deliver,
        'can_return': can_return,
        'max_returnable': str(max_returnable),
    }


def daily_closing_problems(qs):
    """
    ردیف‌هایی که بستن روز را ناامن می‌کنند — کاملاً فقط‌خواندنی.

    هیچ قاعدهٔ کسب‌وکار جدیدی اختراع نمی‌شود؛ هر سه دسته مستقیماً از وضعیت و
    فیلدهای موجود ``DailyMaterialQueue`` (Phase 3 و Phase 6) مشتق می‌شود:

    * ``conflict``          → ``has_plan_conflict`` (همان پرچم Phase 3)
    * ``incomplete``        → ``status='pending'`` با ``planned_quantity > 0``
                              یعنی نیاز برنامه‌ریزی‌شده هنوز تحویل نشده است
    * ``inconsistent``      → وضعیت ذخیره‌شده با مقدارهای تحویل/برگشت نمی‌خواند،
                              دقیقاً مطابق همان چیزی که ``has_transaction``،
                              ``computed_actual_consumption`` و قاعدهٔ گذار وضعیت
                              در ``execute_daily_return`` تعریف کرده‌اند.

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
        if queue.status == 'pending' and planned > 0:
            reasons.add('incomplete')
        # ناسازگاری تراکنش: هر چهار حالت زیر در وضعیت‌های موجود صف معنا دارند
        if returned > delivered:
            reasons.add('inconsistent')
        if queue.status in ('delivered', 'returned') and not queue.has_transaction:
            reasons.add('inconsistent')
        if queue.status == 'returned' and returned < delivered:
            reasons.add('inconsistent')
        if _q2(queue.actual_consumption) != _q2(queue.computed_actual_consumption):
            reasons.add('inconsistent')

        if reasons:
            problems.append({'queue': queue, 'reasons': sorted(reasons)})

    return problems


def daily_closing_status(date, *, worker_id=None, material_id=None):
    """
    «کنترل و بستن روز» برای یک تاریخ کاری — فقط خواندنی.

    هیچ نوشتنی انجام نمی‌دهد: نه ``StockMovement`` می‌سازد، نه تحویل/برگشتی
    اجرا می‌کند و نه مقداری از صف را تغییر می‌دهد. اعداد از همان سرویس‌های
    گزارش Phase 7 خوانده می‌شوند تا تعریف دومی از مصرف ساخته نشود.

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

    incomplete_count = sum(1 for p in problems if 'incomplete' in p['reasons'])
    conflict_count = sum(1 for p in problems if 'conflict' in p['reasons'])
    inconsistent_count = sum(1 for p in problems if 'inconsistent' in p['reasons'])

    return {
        'summary': summary,
        'problems': problems,
        'status': 'closable' if not problems else 'needs_review',
        'closable': not problems,
        'problem_count': len(problems),
        'incomplete_count': incomplete_count,
        'conflict_count': conflict_count,
        'inconsistent_count': inconsistent_count,
    }


def get_daily_closing(date):
    """سند تأیید روز، اگر ثبت شده باشد (فقط خواندنی)."""
    from .models import DailyMaterialClosing

    return DailyMaterialClosing.objects.filter(work_date=date).first()


@transaction.atomic
def confirm_daily_closing(*, date, closed_by, note=''):
    """
    ثبت «روز بررسی و تأیید شد» (Phase 9).

    قوانین:
        * روز دارای مشکل کنترل‌نشده بسته نمی‌شود؛
        * یک روز دوبار بسته نمی‌شود؛
        * هیچ ``StockMovement`` ایجاد نمی‌شود؛
        * هیچ مقدار ``DailyMaterialQueue`` تغییر نمی‌کند.

    وضعیت روز از ``daily_closing_status`` (فقط‌خواندنی) گرفته می‌شود، پس
    قاعدهٔ تشخیص مشکل در یک نقطه می‌ماند.
    """
    from .models import DailyMaterialClosing

    control = daily_closing_status(date)

    if not control['closable']:
        raise HandoverError(
            f'این روز قابل بستن نیست: {control["problem_count"]} ردیف '
            'نیازمند بررسی است (تعارض، ردیف ناقص یا تراکنش ناسازگار).'
        )

    if DailyMaterialClosing.objects.filter(work_date=date).exists():
        raise HandoverError('این روز قبلاً بررسی و تأیید شده است.')

    try:
        closing = DailyMaterialClosing.objects.create(
            work_date=date,
            status='confirmed',
            closed_by=closed_by,
            note=(note or '')[:255],
        )
    except IntegrityError:
        # unique_together روی work_date: دو درخواست هم‌زمان
        raise HandoverError('این روز قبلاً بررسی و تأیید شده است.')

    return closing


def preview_daily_delivery(queue_id):
    """
    Return a read-only preview of what execute_daily_delivery would do
    for the given queue row. No database writes.
    """
    from .models import DailyMaterialQueue

    try:
        queue = DailyMaterialQueue.objects.get(pk=queue_id)
    except DailyMaterialQueue.DoesNotExist:
        raise HandoverError('ردیف صف مواد روزانه یافت نشد.')

    planned = _q2(queue.planned_quantity)
    pack = _q2(queue.raw_material.pack_size or 0)
    packs, physical = _physical_for(planned, pack)

    return {
        'queue': queue,
        'planned': planned,
        'pack_size': pack,
        'packs': packs,
        'physical': physical,
        'stock': _q2(queue.raw_material.current_stock),
        'enough': physical <= _q2(queue.raw_material.current_stock),
        'name': queue.raw_material.name,
        'unit': queue.raw_material.get_unit_display(),
        **daily_queue_action_state(queue),
    }