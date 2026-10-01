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

from django.db import transaction
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