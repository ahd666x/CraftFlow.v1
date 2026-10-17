"""Deterministic shelf nesting helpers. Dimensions are millimetres; decimals are preserved."""
import xml.etree.ElementTree as ET
from decimal import Decimal

NS = 'http://www.King-stone.com'
ET.register_namespace('', NS)


def stock_sort_key(stock, policy='offcuts_first'):
    priority = int(stock.get('priority', 100))
    area = stock['width'] * stock['height']
    stable_id = int(stock.get('stock_id', 0))
    if policy == 'priority':
        return (priority, not stock.get('is_offcut', False), area, stock['width'], stock['height'], stable_id)
    # Default is offcut-first. Priority is the next deterministic tie-breaker.
    return (not stock.get('is_offcut', False), priority, area, stock['width'], stock['height'], stable_id)


def optimize(parts, stock, kerf=4, policy='offcuts_first'):
    bins = sorted(stock, key=lambda s: stock_sort_key(s, policy))
    remaining = sorted((dict(p) for p in parts), key=lambda p: p['width'] * p['height'], reverse=True)
    layouts = []
    for sheet in bins:
        usable_width = max(0, sheet['width'] - 10)
        usable_height = max(0, sheet['height'] - 10)
        placed = []
        left = []
        x = y = row_h = 0
        for part in remaining:
            candidates = [(part['width'], part['height'], False)]
            if part.get('turn', True) and part['width'] != part['height']:
                candidates.append((part['height'], part['width'], True))
            options = []
            for width, height, rotated in candidates:
                if y + height <= usable_height and x + width <= usable_width:
                    options.append((height, width, rotated, x, y, False))
                next_y = y + row_h + (kerf if placed else 0)
                if next_y + height <= usable_height and width <= usable_width:
                    options.append((height, width, rotated, 0, next_y, True))
            if not options:
                left.append(part)
                continue
            height, width, rotated, next_x, next_y, new_row = min(options, key=lambda o: (o[0], o[1], o[2], o[5]))
            if new_row:
                x, y, row_h = 0, next_y, 0
            placed.append({**part, 'x': next_x, 'y': next_y, 'width': width, 'height': height, 'rotated': rotated, 'row': y})
            x = next_x + width + kerf
            row_h = max(row_h, height)
        if placed:
            layouts.append({'sheet': sheet, 'parts': placed})
        remaining = left
        if not remaining:
            break
    if remaining:
        part = remaining[0]
        raise ValueError(f"برای قطعه «{part['label']}» ({part['width']}×{part['height']}) ورق قابل استفاده/موجودی کافی نیست.")
    return layouts


def expected_offcuts(layout, kerf=4, minimum=200):
    """Return disjoint, conservative rectangular remnants; these are forecasts only."""
    sheet = layout['sheet']
    shelves = {}
    for part in layout['parts']:
        shelves.setdefault(part['row'], []).append(part)
    remnants = []
    max_bottom = 0
    for shelf_y, parts in sorted(shelves.items()):
        row_height = max(part['height'] for part in parts)
        used_width = max(part['x'] + part['width'] for part in parts)
        max_bottom = max(max_bottom, shelf_y + row_height)
        rem_width = sheet['width'] - used_width - kerf - 5
        if rem_width >= minimum and row_height >= minimum:
            remnants.append({'width': rem_width, 'height': row_height, 'x': used_width + kerf, 'y': shelf_y})
    bottom_height = sheet['height'] - max_bottom - kerf - 5
    if sheet['width'] >= minimum and bottom_height >= minimum:
        remnants.append({'width': sheet['width'] - 10, 'height': bottom_height, 'x': 0, 'y': max_bottom + kerf})
    return remnants


def make_autocut(plan_id, material_rows, kerf=4):
    root = ET.Element(f'{{{NS}}}AutoCUT', {'ver': '500'})
    project = ET.SubElement(root, f'{{{NS}}}Project', {
        'Name': f'CraftFlow-{plan_id}', 'Selected': '0', 'Update': '0', 'DefaultLevel': '0',
        'UserFields': 'F2,F3,F4,F5,F26,F18,F19,F20,',
        'FieldLabels': 'TLGrain=دسته محصول,TLOrder=نام محصول,TLType=fcfffff,F2=نام قطعه,F3=بارکد,F4=نوار طول 1,F5=نوار طول 2,F26=نوار عرض 1,F18=نوار عرض 2,F19=تحویل به,F20=نام مشتری,',
    })
    for row in material_rows:
        data = ET.SubElement(project, f'{{{NS}}}Data', {'Class': '3', 'TotalUnit': '1000000', 'Type': row['type'], 'UnitPrice': '0', 'Ply': str(row['thickness'])})
        objective = ET.SubElement(data, f'{{{NS}}}Objective', {'Type': 'Shape', 'Count': str(len(row['shapes']))})
        for index, part in enumerate(row['shapes']):
            ET.SubElement(objective, f'{{{NS}}}Shape', {
                'Name': f'P{index + 1:03d}', 'X': str(part['orig_width']), 'Y': str(part['orig_height']),
                'Turn': 'true' if part.get('turn', True) else 'false', 'Grain': part.get('grain', ''),
                'Order': part.get('order', ''), 'Count': '1', 'F2': part.get('f2', ''), 'F3': part.get('f3', ''),
                'F19': part.get('routing', ''), 'F20': part.get('customer', ''),
            })
        condition_sizes = {}
        for layout_data in row['sheets']:
            sheet = layout_data['sheet']
            key = (str(sheet.get('label', row['type'])), str(sheet['width']), str(sheet['height']))
            condition_sizes[key] = condition_sizes.get(key, 0) + 1
        condition = ET.SubElement(data, f'{{{NS}}}Condition', {'Type': 'Shape', 'Count': str(len(condition_sizes))})
        for (name, width, height), count in condition_sizes.items():
            ET.SubElement(condition, f'{{{NS}}}Shape', {
                'Name': name, 'X': width, 'Y': height, 'Turn': 'false',
                'Sx': '5', 'Sy': '5', 'Count': str(count),
            })
        solutions = ET.SubElement(data, f'{{{NS}}}Solutions', {'Selected': '0'})
        total_area = sum(Decimal(str(s['sheet']['width'])) * Decimal(str(s['sheet']['height'])) for s in row['sheets'])
        used_area = sum(Decimal(str(p['width'])) * Decimal(str(p['height'])) for s in row['sheets'] for p in s['parts'])
        solution = ET.SubElement(solutions, f'{{{NS}}}Solution', {
            'SolveGap': str(kerf), 'AlgCase': '1', 'AlgNest': '21', 'SolveRate': '10',
            'UnitCounts': ','.join('1' for _ in row['shapes']) + ',',
            'SheetCounts': ','.join('1' for _ in row['sheets']) + ',',
            'SumUnitArea': str(used_area), 'SumSheetArea': str(total_area),
        })
        layouts = ET.SubElement(solution, f'{{{NS}}}Layouts')
        for index, layout_data in enumerate(row['sheets']):
            sheet = layout_data['sheet']
            area = sum(Decimal(str(part['width'])) * Decimal(str(part['height'])) for part in layout_data['parts'])
            uids = ','.join(str(part['uid']) for part in layout_data['parts']) + ','
            layout = ET.SubElement(layouts, f'{{{NS}}}Layout', {
                'ID': str(index), 'SheetID': str(index), 'Count': '1', 'Cut': 'true', 'Side': '5',
                'SheetName': sheet.get('label', row['type']), 'X': str(sheet['width']), 'Y': str(sheet['height']),
                'SX': '5', 'SY': '5', 'UIDS': uids, 'UnitArea': str(area),
                'SheetArea': str(Decimal(str(sheet['width'])) * Decimal(str(sheet['height']))),
                'SpareArea': str(Decimal(str(sheet['width'])) * Decimal(str(sheet['height'])) - area),
            })
            base = ET.SubElement(layout, f'{{{NS}}}Part', {'X': str(sheet['width']), 'Y': str(sheet['height']), 'Px': '5', 'Py': '5', 'Side': '5'})
            for part in layout_data['parts']:
                attrs = {'X': str(part['width']), 'Y': str(part['height']), 'Px': str(part['x'] + 5), 'Py': str(part['y'] + 5), 'UID': str(part['uid'])}
                if part['rotated']:
                    attrs['Angle'] = '90'
                ET.SubElement(base, f'{{{NS}}}Part', attrs)
        ET.SubElement(solution, f'{{{NS}}}Spares')
    return ET.tostring(root, encoding='utf-16', xml_declaration=True)
