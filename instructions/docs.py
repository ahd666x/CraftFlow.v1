"""گردآوری داده‌ی سندها.

قالب HTML و خروجی Word هر دو از همین ساختار می‌خوانند؛ پس منطق «کدام بند
در کدام ایستگاه بیاید» فقط یک‌جا نوشته شده است.
"""
import re
from collections import Counter, defaultdict

from product.models import STATION_CHOICES, Material

from .cnc_guide import get_active_setup, serialize_setup
from .models import WorkInstruction
from .utils import _item_color_codes, _item_material_ids, models_q_for_item

STATION_LABELS = dict(WorkInstruction.STATION_FILTER_CHOICES)
STATION_SHORT = dict(STATION_CHOICES)
KIND_CODES = {code for code, _ in WorkInstruction.KIND_CHOICES}
KIND_PLURAL = {
    WorkInstruction.KIND_STEP: 'مراحل',
    WorkInstruction.KIND_NOTE: 'نکته‌ها',
    WorkInstruction.KIND_WARNING: 'هشدارها',
    WorkInstruction.KIND_CHECKLIST: 'چک‌لیست‌ها',
    WorkInstruction.KIND_SPEC: 'مشخصات',
}


def parse_hidden_kinds(request):
    """``?hide=note,step`` → {'note', 'step'} (فقط کدهای معتبر)."""
    raw = request.GET.get('hide', '')
    return {k for k in raw.split(',') if k in KIND_CODES}


def safe_filename(text, fallback='document'):
    text = re.sub(r'[\\/:*?"<>|\s]+', '_', (text or '').strip()).strip('_.')
    return text or fallback


def fmt_num(value):
    """۱۹۲۰٫۰ → ۱۹۲۰ و ۳۷۳٫۵ → ۳۷۳٫۵ (بدون صفر اعشاری بی‌مصرف)."""
    if value is None or value == '':
        return '—'
    number = float(value)
    return str(int(number)) if number == int(number) else f'{number:g}'


def kind_summary(instructions):
    counts = Counter(ins.kind for ins in instructions)
    return [
        {'code': code, 'label': KIND_PLURAL[code], 'count': counts[code]}
        for code, _label in WorkInstruction.KIND_CHOICES if counts.get(code)
    ]


def build_sections(instructions, cnc_auto=False):
    """گروه‌بندی بندها به ترتیب مسیر تولید؛ ایستگاه خالی نمایش داده نمی‌شود."""
    grouped = defaultdict(list)
    for ins in instructions:
        grouped[ins.station].append(ins)
    sections = []
    for code in WorkInstruction.STATION_ORDER:
        items = grouped.get(code, [])
        auto = code == 'cnc' and cnc_auto
        if items or auto:
            sections.append({
                'code': code,
                'label': STATION_LABELS.get(code, code),
                'items': items,
                'auto_cnc': auto,
            })
    return sections


def cnc_doc_block():
    """بخش خودکار ایستگاه CNC (+ نقشه‌ی میز)؛ None یعنی راه‌اندازی ثبت نشده."""
    setup = get_active_setup()
    if setup is None:
        return None
    payload = serialize_setup(setup)
    tw, th = float(setup.table_width), float(setup.table_height)

    def place(x, y):
        return {
            'left': round(x / tw * 100, 2) if tw else 0,
            'top': round((th - y) / th * 100, 2) if th else 0,
        }

    points = []
    for point in payload['points']:
        pos = place(point['x'], point['y'])
        points.append({**point, 'left': pos['left'], 'top': pos['top']})
    payload['map'] = {
        'points': points,
        'origin': place(setup.origin_x, setup.origin_y),
        'aspect': round(th / tw, 4) if tw else 1,
    }
    return payload


def routing_chain(code):
    parts = [p.strip().lower() for p in (code or '').split('.') if p.strip()]
    return [STATION_SHORT.get(p, p) for p in parts]


def build_bom(product):
    rows = []
    for row in product.bom.select_related('part__material').order_by('id'):
        part = row.part
        edges = [f'{label}: {getattr(part, field)}'
                 for label, field in (('F26', 'f26'), ('F18', 'f18'), ('F4', 'f4'), ('F5', 'f5'))
                 if getattr(part, field)]
        rows.append({
            'name': part.name or part.f2 or part.f3 or '—',
            'material': part.material.name if part.material_id else '—',
            'size': f'{fmt_num(part.length)}×{fmt_num(part.width)}',
            'edges': edges,
            'chain': routing_chain(part.routing_code),
            'quantity': row.quantity,
        })
    return rows


def build_product_doc(product):
    instructions = list(
        WorkInstruction.objects.active().filter(product=product)
        .select_related('condition_material', 'condition_color')
        .prefetch_related('images'))
    cnc_setup = cnc_doc_block()
    sections = build_sections(instructions, cnc_auto=cnc_setup is not None)
    bom_rows = build_bom(product)
    return {
        'product': product,
        'sections': sections,
        'bom_rows': bom_rows,
        'bom_has_edges': any(r['edges'] for r in bom_rows),
        'cnc_setup': cnc_setup,
        'kind_filters': kind_summary(instructions),
        'total': len(instructions),
        'has_content': bool(sections),
    }


def build_order_doc(order):
    cnc_setup = cnc_doc_block()
    blocks, everything = [], []
    items = (order.items.select_related('product', 'product__category')
             .prefetch_related('ordercolor').order_by('id'))
    for item in items:
        material_ids = _item_material_ids(item)
        colors = _item_color_codes(item)
        instructions = list(
            WorkInstruction.objects.active()
            .applicable_to(material_ids=material_ids, color_codes=colors)
            .filter(models_q_for_item(item))
            .select_related('part', 'condition_material', 'condition_color')
            .prefetch_related('images'))
        everything.extend(instructions)
        sections = build_sections(instructions)
        blocks.append({
            'item': item,
            'product': item.product,
            'sections': sections,
            'materials': list(Material.objects.filter(id__in=material_ids)
                              .values_list('name', flat=True)),
            'colors': colors,
            'has_content': bool(sections),
        })
    return {
        'order': order,
        'blocks': blocks,
        'cnc_setup': cnc_setup,
        'kind_filters': kind_summary(everything),
        'total': len(everything),
    }
