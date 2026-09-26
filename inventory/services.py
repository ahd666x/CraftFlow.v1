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
"""
from collections import OrderedDict
from decimal import Decimal, ROUND_CEILING

from django.db import transaction
from django.utils import timezone

from .models import (
    MaterialHandover,
    MaterialHandoverLine,
    MaterialIssue,
    MaterialLeftover,
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
# محاسبهٔ برنامهٔ تحویل (بدون نوشتن در دیتابیس)
# ---------------------------------------------------------------------------

def build_plans(issues_qty, leftovers):
    """
    issues_qty: list[(MaterialIssue, Decimal)]
    leftovers : dict[raw_material_id -> Decimal]
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
    }


def preview_handover(items):
    """پیش‌نمایش خلاصهٔ تحویل برای نمایش در صفحه (قفل و نوشتنی ندارد)."""
    issues_qty = _load_issues(items, lock=False)
    raw_ids = {issue.raw_material_id for issue, _ in issues_qty}
    leftovers = {
        row.raw_material_id: row.quantity
        for row in MaterialLeftover.objects.filter(raw_material_id__in=raw_ids)
    }
    return [plan_to_dict(p) for p in build_plans(issues_qty, leftovers)]


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
def execute_handover(*, issued_by, items, received_by=None, note=''):
    """
    items: list[(issue_id, Decimal quantity)]
    همهٔ کارها در یک تراکنش انجام می‌شود؛ اگر هر مرحله خطا بدهد هیچ چیزی ثبت نمی‌شود.
    """
    if not items:
        raise HandoverError('هیچ موردی انتخاب نشده است.')

    issues_qty = _load_issues(items, lock=True)

    # قفل ردیف باقی‌مانده هر ماده هم‌زمان نقش mutex دارد: دو انباردار هم‌زمان
    # نمی‌توانند برای یک ماده موجودی را دوبار مصرف کنند.
    raw_ids = sorted({issue.raw_material_id for issue, _ in issues_qty})
    for raw_id in raw_ids:
        MaterialLeftover.objects.get_or_create(raw_material_id=raw_id)
    leftover_rows = {
        row.raw_material_id: row
        for row in MaterialLeftover.objects.select_for_update()
        .filter(raw_material_id__in=raw_ids).order_by('raw_material_id')
    }

    plans = build_plans(issues_qty, {rid: row.quantity for rid, row in leftover_rows.items()})

    short = [p for p in plans if not p['enough']]
    if short:
        details = '؛ '.join(
            f"{p['raw'].name} (نیاز فیزیکی {p['physical']}، موجودی {p['stock']})" for p in short
        )
        raise HandoverError(f'موجودی انبار کافی نیست: {details}')

    handover = MaterialHandover.objects.create(
        issued_by=issued_by, received_by=received_by, note=(note or '')[:255],
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
        if received_by is not None:
            issue.received_by = received_by
        issue.issued_at = now
        issue.handover = handover
        issue.save(update_fields=[
            'issued_quantity', 'status', 'issued_by', 'received_by',
            'issued_at', 'handover',
        ])
        if issue.defect_id and issue.status == 'issued':
            issue.defect.status = 'rework_issued'
            issue.defect.save(update_fields=['status'])

    for plan in plans:
        delta = plan['new_leftover'] - plan['leftover']
        if delta > 0:
            StockMovement.objects.create(
                raw_material=plan['raw'], movement_type='consumption', quantity=delta,
                created_by=issued_by,
                note=f'باقی‌ماندهٔ بسته/قوطی در سالن تولید — تحویل شماره {handover.id}',
            )
        elif delta < 0:
            StockMovement.objects.create(
                raw_material=plan['raw'], movement_type='adjustment', quantity=-delta,
                created_by=issued_by,
                note=f'مصرف از باقی‌ماندهٔ سالن (قبلاً از انبار کسر شده) — تحویل شماره {handover.id}',
            )
        row = leftover_rows[plan['raw_material_id']]
        row.quantity = plan['new_leftover']
        row.save(update_fields=['quantity', 'updated_at'])

    return handover