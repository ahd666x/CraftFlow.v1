"""خروجی Word (.docx) قابل ویرایش از سند دستور تولید.

ورودی همان ساختاری است که ``docs.build_product_doc`` / ``build_order_doc``
می‌سازند؛ پس HTML و Word همیشه یک محتوا دارند. تمام پاراگراف‌ها راست‌به‌چپ
(bidi) و فونت Tahoma هستند تا روی هر سیستمی بدون نصب فونت درست باز شوند.
"""
import io
import logging

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

logger = logging.getLogger(__name__)

FONT = 'Tahoma'
INK = RGBColor(0x1B, 0x24, 0x30)
MUTED = RGBColor(0x6B, 0x76, 0x86)
KIND_COLOR = {
    'step': RGBColor(0x24, 0x57, 0xD6),
    'note': RGBColor(0x6B, 0x76, 0x86),
    'warning': RGBColor(0xC0, 0x36, 0x2C),
    'checklist': RGBColor(0x1F, 0x8A, 0x55),
    'spec': RGBColor(0x0D, 0x7F, 0xA5),
}


# ── ابزارهای سطح‌پایین RTL ──────────────────────────────────────────
def _insert(parent, element, later_tags):
    """درج element قبل از اولین فرزندِ موجود از later_tags (ترتیب اسکیمای OOXML)."""
    parent.insert_element_before(element, *later_tags)


def _rtl_paragraph(paragraph):
    ppr = paragraph._p.get_or_add_pPr()
    if ppr.find(qn('w:bidi')) is None:
        _insert(ppr, OxmlElement('w:bidi'), (
            'w:adjustRightInd', 'w:snapToGrid', 'w:spacing', 'w:ind',
            'w:contextualSpacing', 'w:mirrorIndents', 'w:suppressOverlap', 'w:jc',
            'w:textDirection', 'w:textAlignment', 'w:textboxTightWrap',
            'w:outlineLvl', 'w:divId', 'w:cnfStyle', 'w:rPr', 'w:sectPr', 'w:pPrChange'))


def _style_run(run, size=10.5, bold=False, color=INK):
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    rpr = run._r.get_or_add_rPr()
    rfonts = rpr.find(qn('w:rFonts'))
    if rfonts is not None:
        rfonts.set(qn('w:cs'), FONT)
    if bold:
        _insert(rpr, OxmlElement('w:bCs'), (
            'w:i', 'w:iCs', 'w:caps', 'w:smallCaps', 'w:strike', 'w:dstrike', 'w:outline',
            'w:shadow', 'w:emboss', 'w:imprint', 'w:noProof', 'w:snapToGrid', 'w:vanish',
            'w:webHidden', 'w:color', 'w:spacing', 'w:w', 'w:kern', 'w:position', 'w:sz',
            'w:szCs', 'w:highlight', 'w:u', 'w:effect', 'w:bdr', 'w:shd', 'w:fitText',
            'w:vertAlign', 'w:rtl', 'w:cs', 'w:em', 'w:lang'))
    szcs = OxmlElement('w:szCs')
    szcs.set(qn('w:val'), str(int(size * 2)))
    _insert(rpr, szcs, ('w:highlight', 'w:u', 'w:effect', 'w:bdr', 'w:shd', 'w:fitText',
                        'w:vertAlign', 'w:rtl', 'w:cs', 'w:em', 'w:lang'))
    _insert(rpr, OxmlElement('w:rtl'), ('w:cs', 'w:em', 'w:lang'))


def _par(container, text='', size=10.5, bold=False, color=INK, before=0, after=4, keep_next=False):
    paragraph = container.add_paragraph()
    _rtl_paragraph(paragraph)
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.keep_with_next = keep_next
    if text:
        _style_run(paragraph.add_run(text), size, bold, color)
    return paragraph


def _add(paragraph, text, size=10.5, bold=False, color=INK):
    _style_run(paragraph.add_run(text), size, bold, color)
    return paragraph


def _shade(cell, hex_fill):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_fill)
    tcpr.append(shd)


def _table(document, headers, rows, widths=None):
    table = document.add_table(rows=1, cols=len(headers))
    table.style = 'Table Grid'
    tblpr = table._tbl.tblPr
    _insert(tblpr, OxmlElement('w:bidiVisual'), (
        'w:tblW', 'w:jc', 'w:tblCellSpacing', 'w:tblInd', 'w:tblBorders', 'w:shd',
        'w:tblLayout', 'w:tblCellMar', 'w:tblLook'))
    for cell, title in zip(table.rows[0].cells, headers):
        cell.paragraphs[0]._p.getparent().remove(cell.paragraphs[0]._p)
        _par(cell, title, size=9.5, bold=True, color=MUTED, after=0)
        _shade(cell, 'F2F4F8')
    for row in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, row):
            cell.paragraphs[0]._p.getparent().remove(cell.paragraphs[0]._p)
            _par(cell, str(value), size=10, after=0)
    if widths:
        for row in table.rows:
            for cell, width in zip(row.cells, widths):
                cell.width = Cm(width)
    _par(document, '', after=2)
    return table


def _picture(paragraph, image_field, width_cm=6.5):
    """درج تصویر با تبدیل به PNG (webp/… را python-docx نمی‌شناسد)."""
    from PIL import Image
    try:
        image_field.open('rb')
        with Image.open(image_field) as src:
            src = src.convert('RGB')
            src.thumbnail((1400, 1400))
            buf = io.BytesIO()
            src.save(buf, format='PNG')
        buf.seek(0)
        paragraph.add_run().add_picture(buf, width=Cm(width_cm))
        return True
    except Exception:
        logger.warning('درج تصویر در Word ممکن نشد: %s', getattr(image_field, 'name', '?'), exc_info=True)
        return False
    finally:
        try:
            image_field.close()
        except Exception:
            pass


# ── بخش‌های سند ───────────────────────────────────────────────────
def _heading(document, text, level=2):
    size = {1: 18, 2: 13, 3: 11.5}[level]
    paragraph = document.add_paragraph(style=f'Heading {level}')
    _rtl_paragraph(paragraph)
    paragraph.paragraph_format.space_before = Pt(16 if level == 2 else 10)
    paragraph.paragraph_format.space_after = Pt(6)
    paragraph.paragraph_format.keep_with_next = True
    _style_run(paragraph.add_run(text), size, True, INK)
    return paragraph


def _instruction(document, ins):
    label, _css = ins.badge
    color = KIND_COLOR.get(ins.kind, MUTED)
    head = _par(document, before=6, after=2, keep_next=True)
    _add(head, f'{label}  ', 9.5, True, color)
    _add(head, ins.title, 11, True, INK)
    notes = list(ins.condition_labels)
    if getattr(ins, 'part_id', None) and getattr(ins, 'part', None):
        notes.insert(0, f'قطعه: {ins.part.name}')
    if notes:
        _par(document, '   '.join(notes), size=9, color=MUTED, after=2, keep_next=True)
    for line in ins.body_lines():
        bullet = '☐ ' if ins.kind == 'checklist' else '• '
        _par(document, bullet + line, after=1)
    images = list(ins.images.all())
    if images:
        holder = _par(document, after=2)
        for img in images:
            ok = _picture(holder, img.image)
            if ok and img.caption:
                _add(holder, f' {img.caption}  ', 8.5, False, MUTED)


def _sections(document, sections, hidden, cnc_setup=None):
    for section in sections:
        items = [i for i in section['items'] if i.kind not in hidden]
        auto = section.get('auto_cnc') and cnc_setup
        if not items and not auto:
            continue
        _heading(document, section['label'], 2)
        if auto:
            _cnc_block(document, cnc_setup)
        for ins in items:
            _instruction(document, ins)


def _cnc_block(document, setup):
    _heading(document, f"راهنمای خودکار ماشین: {setup['name']}", 3)
    if setup['tools']:
        _par(document, 'ابزار هر پارکینگ', 9.5, True, MUTED, after=2, keep_next=True)
        _table(document, ['پارکینگ', 'نوع فرز', 'قطر (mm)', 'توضیح'],
               [[t['label'], t['type_label'], t['diameter_display'], t['description'] or '—']
                for t in setup['tools']], widths=[2.5, 4.5, 2.5, 7.5])
    if setup['offset_procedure']:
        _par(document, 'روند آفست‌زدن و قرارگیری قطعه', 9.5, True, MUTED, after=2, keep_next=True)
        for line in setup['offset_procedure']:
            _par(document, '• ' + line, after=1)
    if setup['points']:
        _par(document, f"نقاط فیکس میز (مبدأ G54: X{setup['origin']['x']:g} / Y{setup['origin']['y']:g})",
             9.5, True, MUTED, before=6, after=2, keep_next=True)
        _table(document, ['برچسب', 'نوع', 'X (mm)', 'Y (mm)', 'شعاع ممنوع'],
               [[p['label'], p['kind_label'], f"{p['x']:g}", f"{p['y']:g}", p['radius']]
                for p in setup['points']], widths=[3.5, 4, 3, 3, 3])
    if setup['notes']:
        _par(document, 'توضیحات عمومی', 9.5, True, MUTED, before=6, after=2, keep_next=True)
        for line in setup['notes']:
            _par(document, '• ' + line, after=1)


def _bom(document, rows, has_edges):
    _heading(document, 'فرمول ساخت (قطعات محصول)', 2)
    headers = ['قطعه', 'متریال', 'ابعاد (mm)'] + (['نوار لبه'] if has_edges else []) + ['مسیر', 'تعداد']
    body = []
    for r in rows:
        row = [r['name'], r['material'], r['size']]
        if has_edges:
            row.append('، '.join(r['edges']) or '—')
        row += [' ← '.join(r['chain']) or '—', r['quantity']]
        body.append(row)
    _table(document, headers, body)


def _new_document(title):
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    for side in ('left_margin', 'right_margin', 'top_margin', 'bottom_margin'):
        setattr(section, side, Cm(1.8))
    document.core_properties.title = title
    document.styles['Normal'].font.name = FONT
    return document


def _to_buffer(document):
    buf = io.BytesIO()
    document.save(buf)
    buf.seek(0)
    return buf


def render_product_docx(data, hidden_kinds=()):
    product = data['product']
    hidden = set(hidden_kinds)
    document = _new_document(f'دستور تولید — {product.name}')
    _par(document, f'دستور تولید — {product.name}', 18, True, before=0, after=2)
    meta = [f'دسته: {product.category.name}' if getattr(product, 'category', None) else '']
    size = getattr(product, 'default_size_display', '')
    if size:
        meta.append(f'ابعاد پیش‌فرض (cm): {size}')
    _par(document, '   '.join(m for m in meta if m), 9.5, color=MUTED, after=8)
    _sections(document, data['sections'], hidden, data.get('cnc_setup'))
    if data['bom_rows']:
        _bom(document, data['bom_rows'], data['bom_has_edges'])
    return _to_buffer(document)


def render_order_docx(data, hidden_kinds=()):
    order = data['order']
    hidden = set(hidden_kinds)
    title = f'دستور تولید سفارش {order.id}' + (f' ({order.number})' if order.number else '')
    document = _new_document(title)
    _par(document, title, 18, True, after=2)
    customer = getattr(getattr(order, 'customer', None), 'name', '') or '—'
    _par(document, f'مشتری: {customer}   تحویل: {order.due_date or "—"}', 9.5, color=MUTED, after=8)
    if data.get('cnc_setup'):
        _heading(document, 'ایستگاه CNC — راه‌اندازی ماشین (مشترک همه آیتم‌ها)', 2)
        _cnc_block(document, data['cnc_setup'])
    for block in data['blocks']:
        item = block['item']
        _heading(document, f'آیتم {item.id} — {block["product"].name}', 2)
        facts = [f'تعداد: {item.quantity}']
        if item.size_display:
            facts.append(f'سایز: {item.size_display}')
        facts += [f'متریال: {m}' for m in block['materials']]
        facts += [f'رنگ کد: {c}' for c in block['colors']]
        _par(document, '   '.join(facts), 9.5, color=MUTED, after=4)
        if not block['has_content']:
            _par(document, 'برای این آیتم دستورالعملی که با متریال/رنگ سفارش بخواند ثبت نشده است.',
                 color=MUTED)
        _sections(document, block['sections'], hidden)
    return _to_buffer(document)
