"""سریال‌سازی دستورالعمل‌های applicable برای UI اپراتور (صفحه اسکن و سند چاپی).

این ماژول فقط به مدل‌های محصول وابسته است تا هم از ``product.views`` و هم
از ویوهای خود اپ بدون حلقه‌ی ایمپورت قابل استفاده باشد.
"""
from .models import ChecklistState, WorkInstruction


def serialize_instruction(instruction):
    """دیکشنری آماده‌ی JSON برای کارت دستورالعمل در صفحه اسکن."""
    return {
        'id': instruction.pk,
        'kind': instruction.kind,
        'badge': list(instruction.badge),
        'title': instruction.title,
        'body_lines': instruction.body_lines(),
        'conditions': instruction.condition_labels,
        'part_id': instruction.part_id,
        'part_name': instruction.part.name if instruction.part_id and instruction.part else '',
        'images': [
            {'url': img.image.url, 'caption': img.caption}
            for img in instruction.images.all()
        ],
    }


def _item_material_ids(order_item):
    """متریال واقعی قطعات آیتم سفارش.

    مبنای درست، قطعاتِ تسک‌های صادرشده است (متریال نهایی بعد از override
    رنگ). اگر تسکی صادر نشده باشد، متریال فرمول ساخت (BOM) استفاده می‌شود.
    """
    from product.models import ProductionTask

    ids = set(ProductionTask.objects.filter(order_item=order_item)
              .exclude(part__isnull=True)
              .values_list('part__material_id', flat=True))
    if not ids:
        ids = set(order_item.product.bom.exclude(part__isnull=True)
                  .values_list('part__material_id', flat=True))
    return ids


def _item_color_codes(order_item):
    """کدهای رنگ واقعی سفارش؛ مقدارهای nan/خالی بی‌معنی و حذف می‌شوند."""
    return [row.code for row in order_item.ordercolor.all() if row.code and row.code != 'nan']


def collect_item_instructions(order_item, station=None, with_checklist=False):
    """فهرست سریال‌شده‌ی دستورالعمل‌های applicable به یک آیتم واقعی.

    - ایستگاه داده شود → همان ایستگاه + دستورالعمل‌های عمومی.
    - شرط متریال/رنگ با واقعیت سفارش مقایسه می‌شود.
    - خروجی بر اساس ترتیب ثبت‌شده و نوع مرتب است؛ عکس‌ها از قبل لود شده‌اند.
    - ``with_checklist`` → برای بندهای چک‌لیستی، فهرست خط‌های تیک‌خورده‌ی
      ماندگار همان آیتم هم در ``checked_lines`` می‌آید (فاز ۳).
    """
    if order_item is None or order_item.product_id is None:
        return []  # تسک‌های بدون آیتم سفارش (کارت آزاد نقاشی) دستورالعمل محصول ندارند
    qs = (WorkInstruction.objects
          .active()
          .applicable_to(
              material_ids=_item_material_ids(order_item),
              color_codes=_item_color_codes(order_item),
              station=station,
          )
          .filter(models_q_for_item(order_item))
          .select_related('part')
          .prefetch_related('images'))
    items = [serialize_instruction(ins) for ins in qs]
    if with_checklist:
        checked = checklist_checked_lines(order_item)
        for row in items:
            if row['kind'] == WorkInstruction.KIND_CHECKLIST:
                row['checked_lines'] = sorted(checked.get(row['id'], ()))
    return items


def checklist_checked_lines(order_item):
    """نقشه‌ی {شناسه دستورالعمل: مجموعه شماره خط‌های تیک‌خورده} برای آیتم."""
    result = {}
    rows = (ChecklistState.objects
            .filter(order_item=order_item)
            .values_list('instruction_id', 'line_index'))
    for instruction_id, line_index in rows:
        result.setdefault(instruction_id, set()).add(line_index)
    return result


def models_q_for_item(order_item):
    """دستورالعمل محصول‌محور + دستورالعمل قطعه‌محور قطعات همین آیتم.

    در فاز ۱ (سطح محصول) همه‌ی دستورالعمل‌های محصول می‌آیند؛ دستورالعمل‌های
    قطعه‌محور فقط برای قطعاتی که در تسک‌های آیتم هستند نمایش داده می‌شوند.
    """
    from django.db.models import Q

    from product.models import ProductionTask

    part_ids = list(ProductionTask.objects.filter(order_item=order_item)
                    .exclude(part__isnull=True)
                    .values_list('part_id', flat=True))
    q = Q(product_id=order_item.product_id)
    if part_ids:
        q |= Q(part_id__in=part_ids)
    return q
