"""Honest toolpath preview for stored, adapter-verified sheet programs.

Reads ONLY the ``gcode`` saved on a :class:`~cnc_resizer.models.CncSheetPlan`
(the byte-exact output of the dialect-locked ``minimal_xy_router``
postprocessor) and reconstructs the machine's XY toolpaths for visual
inspection on the sheet-plans screen. Nothing is rebuilt, re-resolved or
invented here: if the plan has no stored program there is nothing to preview
and the caller (``views.sheet_preview``) refuses with a 409 — fail-closed.

Output shape (JSON-safe, coordinates in CNC space, Y up, mm):

``sheet``          sheet width/height from the plan record
``boxes``          nested part rectangles (bottom-left x/y, label, rotated)
``tools``          every tool number the program loads
``tool_changes``   {line, tool} events (1-based program line numbers)
``paths``          merged polylines: {k: cut|rapid, tool, z, f, risk, pts}
                   ``risk`` marks an XY rapid executed at Z<=0 — i.e. the
                   tool dragging through stock height instead of retracting
``stats``          cut/rapid polyline lengths, plunge count, distinct depths,
                   flagged risk count, non-empty program line count

The dialect whitelist is enforced on the way through: any line outside the
verified vocabulary raises :class:`SheetPreviewError` and the endpoint turns
it into a 409 instead of a partial/lying picture.
"""
from __future__ import annotations

import math

from . import gcode_sheet

_MOTION_G = frozenset({0, 1, 2, 3})
_ALLOWED_G = frozenset({0, 1, 2, 3, 21, 43, 53, 54, 90})
_ALLOWED_M = frozenset({3, 6, 30, 141})
_ALLOWED_LETTERS = frozenset("GMXYZIJFSTH")
_ARC_STEP_DEG = 6.0
_ARC_MAX_POINTS = 96
_EPS = 1e-6


class SheetPreviewError(ValueError):
    """Stored program cannot be rendered honestly as toolpaths."""


def _round(point):
    return [round(point[0], 3), round(point[1], 3)]


def _dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _arc_points(start, end, i, j, clockwise):
    """Sample an incremental-I/J arc into polyline points (start excluded)."""
    cx, cy = start[0] + i, start[1] + j
    r0 = _dist(start, (cx, cy))
    r1 = _dist(end, (cx, cy))
    if abs(r0 - r1) > 0.05:
        raise SheetPreviewError(
            f"قوس ناسازگار در برنامهٔ ذخیره‌شده (شعاع آغاز {r0:.3f} در برابر پایان {r1:.3f})؛ "
            "پیش‌نمایش صادقانه ممکن نشد.")
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)
    sweep = ((a0 - a1) if clockwise else (a1 - a0)) % (2 * math.pi)
    if sweep < _EPS:  # start == end means a full circle
        sweep = 2 * math.pi
    steps = max(8, min(_ARC_MAX_POINTS, int(math.ceil(math.degrees(sweep) / _ARC_STEP_DEG))))
    return [(cx + r0 * math.cos(a0 + (-sweep if clockwise else sweep) * step / steps),
             cy + r0 * math.sin(a0 + (-sweep if clockwise else sweep) * step / steps))
            for step in range(1, steps + 1)]


def sheet_toolpaths(plan):
    """Parse the plan's stored program into renderable toolpath data."""
    text = plan.gcode or ""
    if not text.strip():
        raise SheetPreviewError("برنامهٔ دستگاهی برای پیش‌نمایش ذخیره نشده است.")
    sheet_w, sheet_h = float(plan.width), float(plan.height)

    boxes = []
    for piece in (plan.layout or {}).get('parts', []):
        try:
            x, y = float(piece['x']), float(piece['y'])
            w, h = float(piece['width']), float(piece['height'])
        except (KeyError, TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        boxes.append({'x': x, 'y': y, 'w': w, 'h': h,
                      'label': piece.get('label') or piece.get('f3') or '',
                      'rotated': bool(piece.get('rotated'))})

    state = {'x': 0.0, 'y': 0.0, 'z': None, 'tool': None, 'f': None,
             'motion': None, 'line': 0}
    paths = []
    tools = []
    tool_changes = []
    stats = {'cut_mm': 0.0, 'rapid_mm': 0.0, 'plunges': 0, 'risks': 0,
             'depths': [], 'lines': 0}

    def same_target(entry, kind, tool, z, f, risk):
        if entry is None or entry['k'] != kind or entry['tool'] != tool:
            return False
        if kind == 'cut':
            return (abs((entry['z'] if entry['z'] is not None else 0.0)
                        - (z if z is not None else 0.0)) <= _EPS
                    and entry['f'] == f and entry['risk'] == risk)
        return entry['z'] == z and entry['risk'] == risk

    def add_length(kind, start, end):
        if kind == 'cut':
            stats['cut_mm'] += _dist(start, end)
        else:
            stats['rapid_mm'] += _dist(start, end)

    def append_motion(kind, end_xy, z, f, risk):
        """Extend the last matching polyline or start a new one, counting mm."""
        target = _round(end_xy)
        last = paths[-1] if paths else None
        if last is not None and same_target(last, kind, state['tool'], z, f, risk):
            if _dist(last['pts'][-1], target) <= _EPS:
                return
            last['pts'].append(target)
            add_length(kind, last['pts'][-2], target)
            return
        entry = {'k': kind, 'tool': state['tool'], 'z': z, 'f': f,
                 'risk': risk, 'pts': []}
        # the polyline always starts at the machine's current position —
        # including the program's very first travel from (0,0)
        origin = _round((state['x'], state['y']))
        if _dist(origin, target) > _EPS:
            entry['pts'].append(origin)
            add_length(kind, origin, target)
        entry['pts'].append(target)
        paths.append(entry)

    for raw in text.splitlines():
        state['line'] += 1
        line = gcode_sheet._clean(raw)
        if not line or line == '%' or line in ('M2', 'M02', 'M30'):
            continue
        stats['lines'] += 1
        words = gcode_sheet._words(line)
        letters = {letter for letter, _v, _r in words}
        if letters - _ALLOWED_LETTERS:
            raise SheetPreviewError(
                f"خط {state['line']} حرف خارج از دیالکت تأییدشده دارد («{line}»).")
        g_codes, m_codes = [], []
        values = {}
        for letter, value, _raw in words:
            if letter == 'G':
                if not value.is_integer() or int(value) not in _ALLOWED_G:
                    raise SheetPreviewError(
                        f"خط {state['line']} کد G خارج از دیالکت دارد («{line}»).")
                g_codes.append(int(value))
            elif letter == 'M':
                if not value.is_integer() or int(value) not in _ALLOWED_M:
                    raise SheetPreviewError(
                        f"خط {state['line']} کد M خارج از دیالکت دارد («{line}»).")
                m_codes.append(int(value))
            else:
                values[letter] = value

        if 53 in g_codes:
            # Machine-coordinate park/retract — not part of the sheet path.
            if 'Z' in values:
                state['z'] = None
            continue
        if 6 in m_codes:
            tool = int(values['T']) if 'T' in values else state['tool']
            if 'T' in values and tool not in tools:
                tools.append(tool)
            # only mid-program switches count — the header's first tool block
            # is a setup, not a change
            if state['tool'] is not None and tool != state['tool']:
                tool_changes.append({'line': state['line'], 'tool': tool})
            state['tool'] = tool
            continue
        # NOTE: M3 (spindle) rides along on motion lines in this dialect
        # ("G0X10Y10Z5M3 S16000") — it must never swallow the line's motion.
        # Only a bare M141 / G43 H# setup line is skipped outright.
        if 141 in m_codes and not g_codes:
            continue
        if 43 in g_codes and not ({'X', 'Y', 'Z', 'I', 'J'} & letters):
            continue
        if 'T' in values and not g_codes:
            state['tool'] = int(values['T'])
            if state['tool'] not in tools:
                tools.append(state['tool'])
            continue
        if 'F' in values:
            state['f'] = values['F']
        has_axis = bool({'X', 'Y', 'Z', 'I', 'J'} & letters)
        motion = next((code for code in g_codes if code in _MOTION_G), None)
        if motion is None and not has_axis:
            continue  # setup/unit/spindle-only line
        motion = motion if motion is not None else state['motion']
        if motion is None or not has_axis:
            continue
        if motion not in _MOTION_G:
            raise SheetPreviewError(
                f"خط {state['line']} حرکت خارج از دیالکت دارد («{line}»).")
        state['motion'] = motion
        start = (state['x'], state['y'])
        new_x = values.get('X', state['x'])
        new_y = values.get('Y', state['y'])
        new_z = values.get('Z', state['z'])
        xy_move = _dist(start, (new_x, new_y)) > _EPS
        z_move = state['z'] is not None and new_z is not None \
            and abs(new_z - state['z']) > _EPS
        if motion == 0:
            if xy_move:
                risk = new_z is not None and new_z <= 0.0
                append_motion('rapid', (new_x, new_y), new_z, None, risk)
                if risk:
                    stats['risks'] += 1
            elif z_move and new_z < state['z']:
                stats['plunges'] += 1
        elif motion in (2, 3):
            if 'I' not in values or 'J' not in values:
                raise SheetPreviewError(
                    f"خط {state['line']} قوس بدون مرکز I/J دارد («{line}»).")
            for point in _arc_points(start, (new_x, new_y),
                                     values['I'], values['J'], motion == 2):
                append_motion('cut', point, new_z, state['f'], False)
        elif xy_move:
            append_motion('cut', (new_x, new_y), new_z, state['f'], False)
        elif z_move and new_z < state['z']:
            stats['plunges'] += 1
        state['x'], state['y'], state['z'] = new_x, new_y, new_z
        if new_z is not None and new_z < 0.0:
            depth = round(new_z, 3)
            if depth not in stats['depths']:
                stats['depths'].append(depth)

    for path in paths:
        if path['tool'] is None:
            path['tool'] = 0
    return {
        'plan_id': plan.pk,
        'adapter': (plan.layout or {}).get('adapter', ''),
        'sheet': {'width': sheet_w, 'height': sheet_h},
        'boxes': boxes,
        'tools': sorted(tools),
        'tool_changes': tool_changes,
        'paths': paths,
        'stats': {
            'cut_mm': round(stats['cut_mm'], 1),
            'rapid_mm': round(stats['rapid_mm'], 1),
            'plunges': stats['plunges'],
            'risks': stats['risks'],
            'depths': sorted(stats['depths']),
            'lines': stats['lines'],
        },
    }
