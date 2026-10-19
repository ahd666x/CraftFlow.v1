"""فاز B موتور چیدمان — چند الگوریتم موازی با مقایسهٔ بهره‌وری (استاندارد AutoSAW).

قرارداد مشترک: خروجی هر موتور دقیقاً هم‌شکل خروجی optimizer.optimize است:
    [{'sheet': <stock dict>, 'parts': [<part + x, y, width, height, rotated, row>]}]
تا expected_offcuts، make_autocut، نمای جزئیات و شبیه‌ساز بدون تغییر کار کنند.

قواعد:
- ابعاد میلی‌متر و Decimal حفظ می‌شود؛ هیچ گردکردنی رخ نمی‌دهد.
- kerf (ضخامت تیغه) بین هر دو قطعه/برش درج می‌شود.
- رگه (grain): اگر respect فعال باشد، قطعهٔ رگه‌دار هرگز نمی‌چرخد (turn=False).
- سربر (trim): نوار مستقل از لبهٔ ابتدای ورق (محور طول) کسر می‌شود و در
  ترتیب برش به‌عنوان نخستین برش «سربر» می‌آید.
- پیام خطای «ورق کافی نیست» عیناً همان قالب optimizer است، چون planner برای
  حذف تسک ناسازگار روی برچسب قطعه در متن خطا match می‌زند.
"""
from decimal import Decimal

from .optimizer import stock_sort_key


NOT_ENOUGH_SHEET = "برای قطعه «{label}» ({width}×{height}) ورق قابل استفاده/موجودی کافی نیست."

ENGINE_LABELS = {
    'shelf': 'قفسه‌ای FFDH (پایه)',
    'guillotine': 'گیلوتین SAS',
    'skyline': 'اسکایلاین Bottom-Left',
}
ENGINE_ORDER = ('shelf', 'guillotine', 'skyline')


def apply_grain(parts):
    """رگه‌دارها را بدون چرخش برمی‌گرداند؛ ورودی دست‌نخورده می‌ماند."""
    result = []
    for part in parts:
        piece = dict(part)
        if (str(piece.get('grain') or '').strip()) and piece.get('turn', True):
            piece['turn'] = False
        result.append(piece)
    return result


def _dec(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _sheet_geometry(sheet, trim):
    """حاشیهٔ ثابت ۵mm هر طرف (مطابق optimizer) + سربر مستقل روی لبهٔ طول."""
    usable_width = max(Decimal('0'), _dec(sheet['width']) - Decimal('10'))
    usable_height = max(Decimal('0'), _dec(sheet['height']) - Decimal('10') - max(Decimal('0'), _dec(trim)))
    return usable_width, usable_height


def _no_fit_error(part):
    return ValueError(NOT_ENOUGH_SHEET.format(label=part.get('label', ''), width=part['width'], height=part['height']))


def _candidates(part):
    out = [(part['width'], part['height'], False)]
    if part.get('turn', True) and part['width'] != part['height']:
        out.append((part['height'], part['width'], True))
    return out


def _finish(sheet, placements):
    parts = []
    for width, height, rotated, x, y, part in placements:
        parts.append({**part, 'x': x, 'y': y, 'width': width, 'height': height,
                      'rotated': rotated, 'row': y})
    return {'sheet': dict(sheet), 'parts': parts}


def optimize_guillotine(parts, stock, kerf=4, policy='offcuts_first', trim=0):
    """گیلوتین SAS: اولین‌جای مناسب با انتخاب مستطیل آزاد کم‌اتلاف‌تر؛ برش‌های سرتاسری."""
    kerf, trim = _dec(kerf), _dec(trim)
    bins = sorted(stock, key=lambda s: stock_sort_key(s, policy))
    remaining = sorted((dict(p) for p in parts), key=lambda p: p['width'] * p['height'], reverse=True)
    layouts = []
    for sheet in bins:
        usable_width, usable_height = _sheet_geometry(sheet, trim)
        free = [(Decimal('0'), Decimal('0'), usable_width, usable_height)]
        placed, left = [], []
        for part in remaining:
            choice = None
            for index, (x, y, rw, rh) in enumerate(free):
                for width, height, rotated in _candidates(part):
                    if width <= rw and height <= rh:
                        leftover = (rw - width) + (rh - height)
                        if choice is None or leftover < choice[0]:
                            choice = (leftover, index, width, height, rotated, x, y)
            if choice is None:
                left.append(part)
                continue
            _, index, width, height, rotated, x, y = choice
            placed.append((width, height, rotated, x, y, part))
            rx, ry, rw, rh = free[index]
            right_w = rw - width - kerf
            top_h = rh - height - kerf
            free[index] = (x + width + kerf, y, right_w, height) if right_w > 0 else None
            free.append((x, y + height + kerf, rw, top_h) if top_h > 0 else None)
            free = [rect for rect in free if rect]
        if placed:
            layouts.append(_finish(sheet, placed))
        remaining = left
        if not remaining:
            break
    if remaining:
        raise _no_fit_error(remaining[0])
    return layouts


def optimize_skyline(parts, stock, kerf=4, policy='offcuts_first', trim=0):
    """اسکایلاین Bottom-Left: خط افق بالای قطعات؛ اولین کم‌ارتفاع‌ترین جای مجاز."""
    kerf, trim = _dec(kerf), _dec(trim)
    bins = sorted(stock, key=lambda s: stock_sort_key(s, policy))
    remaining = sorted((dict(p) for p in parts), key=lambda p: p['width'] * p['height'], reverse=True)
    layouts = []
    for sheet in bins:
        usable_width, usable_height = _sheet_geometry(sheet, trim)
        skyline = [(Decimal('0'), Decimal('0'), usable_width)]  # (x, y, width)
        placed, left = [], []
        for part in remaining:
            choice = None
            for width, height, rotated in _candidates(part):
                for index, (sx, sy, sw) in enumerate(skyline):
                    if width > sw:
                        continue
                    span_end = sx + width
                    top = Decimal('0')
                    merged = Decimal('0')
                    for x2, y2, w2 in skyline:
                        if x2 + w2 <= sx or x2 >= span_end:
                            continue
                        top = max(top, y2)
                        merged += w2
                    if merged < width:  # شکاف تا انتهای بازه پوشش داده نمی‌شود
                        continue
                    if top + height > usable_height:
                        continue
                    key = (top + height, sx, rotated)
                    if choice is None or key < choice[0]:
                        choice = (key, index, width, height, rotated, sx, top)
            if choice is None:
                left.append(part)
                continue
            _, index, width, height, rotated, x, y = choice
            placed.append((width, height, rotated, x, y, part))
            rebuilt = []
            for sx, sy, sw in skyline:
                start, end = sx, sx + sw
                if end <= x or start >= x + width:
                    rebuilt.append((sx, sy, sw)); continue
                if start < x:
                    rebuilt.append((start, sy, x - start))
                if end > x + width:
                    # باقیماندهٔ راستِ سگمنتِ پوشیده‌شده در همان ارتفاع قدیمی می‌ماند
                    rebuilt.append((x + width, sy, end - (x + width)))
            rebuilt.append((x, y + height + kerf, width))  # سکوی بالای قطعهٔ تازه
            skyline = sorted((seg for seg in rebuilt if seg[2] > 0), key=lambda seg: seg[0])
        if placed:
            layouts.append(_finish(sheet, placed))
        remaining = left
        if not remaining:
            break
    if remaining:
        raise _no_fit_error(remaining[0])
    return layouts


def run_engine(name, parts, stock, kerf=4, policy='offcuts_first', trim=0, respect_grain=False):
    """اجرای یک موتور با قرارداد یکسان؛ shelf همان optimizer.optimize مرجع است."""
    if name not in ENGINE_LABELS:
        raise ValueError(f'الگوریتم «{name}» شناخته‌شده نیست.')
    if respect_grain:
        parts = apply_grain(parts)
    if name == 'shelf':
        from .optimizer import optimize
        return optimize(parts, stock, kerf, policy=policy)
    if name == 'guillotine':
        return optimize_guillotine(parts, stock, kerf, policy=policy, trim=trim)
    return optimize_skyline(parts, stock, kerf, policy=policy, trim=trim)


def utilization_percent(layouts):
    total_sheet = sum(_dec(s['sheet']['width']) * _dec(s['sheet']['height']) for s in layouts)
    total_piece = sum(_dec(p['width']) * _dec(p['height']) for s in layouts for p in s['parts'])
    return (total_piece / total_sheet * 100) if total_sheet else Decimal('0')


def compare_engines(parts, stock, kerf=4, policy='offcuts_first', names=None, trim=0, respect_grain=False):
    """اجرای همهٔ موتورهای خواسته‌شده و برگرداندن نتایج مقایسه + چیدمان هر موتور.

    خروجی: (results, chosen_name) — results مرتب بر پایهٔ ENGINE_ORDER است و هر
    عضو شامل {'algorithm','label','ok','sheet_count','utilization','layouts','error'}.
    وقتی هیچ موتوری جا نزند، نخستین خطا (به قالب مرجع) بالا می‌رود تا planner
    بتواند تسک ناسازگار را با همان منطق قدیمی حذف کند.
    """
    names = [name for name in (names or ENGINE_ORDER) if name in ENGINE_LABELS] or ['shelf']
    results = []
    first_error = None
    for name in names:
        try:
            layouts = run_engine(name, parts, stock, kerf, policy=policy, trim=trim, respect_grain=respect_grain)
            results.append({
                'algorithm': name, 'label': ENGINE_LABELS[name], 'ok': True,
                'sheet_count': len(layouts), 'utilization': utilization_percent(layouts),
                'layouts': layouts, 'error': '',
            })
        except (ValueError, TypeError) as exc:
            if first_error is None:
                first_error = exc
            results.append({
                'algorithm': name, 'label': ENGINE_LABELS[name], 'ok': False,
                'sheet_count': None, 'utilization': None, 'layouts': None, 'error': str(exc),
            })
    ok_rows = [row for row in results if row['ok']]
    if not ok_rows:
        raise first_error
    chosen = min(ok_rows, key=lambda row: (row['sheet_count'], -row['utilization'], ENGINE_ORDER.index(row['algorithm'])))
    return results, chosen['algorithm']


def build_cut_sequence(layout, kerf=4, trim=0):
    """ترتیب برش واقعی هر ورق (مرجع سرور) — همان منطق شبیه‌ساز، اما قطعی و ذخیره‌شدنی.

    ترتیب: سربر اختیاری → برای هر طبقه از پایین به بالا: برش‌های عرضی (cross)
    بین قطعات، سپس برش طولی (rip) جداکنندهٔ طبقه. قطعات با uid ارجاع می‌شوند تا
    شماره‌گذاری نمایشی (no) بعداً در view جایگزین شود.
    """
    kerf, trim = _dec(kerf), _dec(trim)
    parts = sorted(layout['parts'], key=lambda p: (_dec(p['y']), _dec(p['x'])))
    rows = []
    for part in parts:
        y = _dec(part['y'])
        row = next((item for item in rows if abs(item['y'] - y) < Decimal('0.05')), None)
        if row:
            row['parts'].append(part)
        else:
            rows.append({'y': y, 'parts': [part]})
    sheet_height = _dec(layout['sheet']['height'])
    sheet_width = _dec(layout['sheet']['width'])
    sequence = []
    if trim > 0:
        sequence.append({'t': 'trim', 'axis': 'y', 'at': trim, 'x': sheet_width / 2, 'pieces': []})
    for index, row in enumerate(rows):
        ordered = sorted(row['parts'], key=lambda p: _dec(p['x']))
        for position, part in enumerate(ordered):
            if position < len(ordered) - 1:
                sequence.append({
                    't': 'cross', 'axis': 'x',
                    'at': _dec(part['x']) + _dec(part['width']) + kerf / 2,
                    'y': _dec(part['y']) + _dec(part['height']) / 2,
                    'pieces': [part['uid']],
                })
        row_height = max(_dec(p['height']) for p in ordered)
        bottom = row['y'] + row_height
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        rip_y = (_dec(next_row['parts'][0]['y']) - kerf / 2) if next_row else bottom + kerf / 2
        sequence.append({
            't': 'rip', 'axis': 'y', 'at': min(rip_y, sheet_height),
            'x': sheet_width / 2,
            'pieces': [part['uid'] for part in [ordered[-1], *reversed(ordered[:-1])]],
        })
    return [
        {'t': cut['t'], 'axis': cut['axis'],
         'at': format(_dec(cut['at']), 'f'),
         'x': format(_dec(cut.get('x', sheet_width / 2)), 'f'),
         'pieces': [int(uid) for uid in cut['pieces']]}
        for cut in sequence
    ]
