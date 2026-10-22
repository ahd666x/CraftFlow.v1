# -*- coding: utf-8 -*-
"""
core.py - pure-Python logic for resizing the LENGTH (Y axis) of a CNC panel.

No Django (or any third-party) import here, so it can be used from a Django
view, a script or a test.  Main entry point:

    process(text, params) -> dict (JSON-serialisable)

What it does (same idea as resize_cnc_length.py, but the distribution of the
change between the hole rows is now adjustable):

  * detects the hole rows ("vadar" holes) and the two long straight edges;
  * the length between them is split into "gaps"
        edge -> row1 -> row2 -> ... -> edge
    the change (delta) is distributed over these gaps - equally, proportionally
    to their length, or by hand - and every Y in the file is moved by the shift
    of the zone it falls in.  X, angles, arc radii and hole shapes never change;
  * returns the vectors (outline + hole circles) before/after for drawing, the
    new .cnc text and a DXF of the new vectors.
"""
import json
import logging
import math
import re

log = logging.getLogger(__name__)

NUM = r'[+-]?\d+(?:\.\d+)?'
TAU = 2 * math.pi
HOLE_MAX_SIZE = 5.0        # closed toolpath loops smaller than this (mm) are holes
MAX_BYTES = 2 * 1024 * 1024   # Django's default request limit is 2.5 MB

_COMMENT_PAREN_RE = re.compile(r'\([^)]*\)')


class CncError(Exception):
    """Problem with the input the user should be told about (message is Persian)."""


# ---------------------------------------------------------------- parsing
def split_lines(text):
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines, eol


def _code_only(line):
    """The line's executable part: (...)-comments and after-; dropped.

    get_val used to search the RAW line, so ``G1 X10 (see Y5.5)`` reported
    Y=5.5 and ``(T9 tool) G1 X1 Y2`` reported T=9 — comment words leaked into
    geometry and metadata (archive-review bug #4)."""
    s = line.split(';', 1)[0]
    if '(' in s:
        s = _COMMENT_PAREN_RE.sub(' ', s)
    return s


def get_val(line, letter):
    m = re.search(letter + '(' + NUM + ')', _code_only(line))
    return float(m.group(1)) if m else None


def _sub_word_outside_comments(raw, letter, replacement):
    """Replace the FIRST ``<letter><number>`` word that lives OUTSIDE (...)
    and ;-comment spans; return the line unchanged when there is none.

    ``apply_resize`` used ``re.sub(..., count=1)`` on the raw line, so a
    leading comment's Y was rewritten instead of the real word."""
    spans = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == '(':
            close = raw.find(')', i + 1)
            if close == -1:
                spans.append((i, n))
                break
            spans.append((i, close + 1))
            i = close + 1
        elif ch == ';':
            spans.append((i, n))
            break
        else:
            i += 1
    for m in re.finditer(letter + '(' + NUM + ')', raw):
        if any(s <= m.start() < e for s, e in spans):
            continue
        return raw[:m.start()] + replacement + raw[m.end():]
    return raw


def _blocks(lines):
    """Split the program into blocks started by a rapid (G0) XY move.
    Each block: dict(start=(x,y), segs=[dict(type, p0, p1, center, cw)],
    plus modal metadata (z depth, feed, spindle, tool) observed inside it."""
    x = y = z = 0.0
    g = None
    feed = spindle = tool = None
    blocks, cur = [], None
    for raw in lines:
        s = raw.strip()
        if not s or s == "%" or "G53" in s:
            continue
        for gm in re.finditer(r'G(\d+)', s):
            gv = int(gm.group(1))
            if gv in (0, 1, 2, 3):
                g = gv
        gx, gy, gz = get_val(s, 'X'), get_val(s, 'Y'), get_val(s, 'Z')
        i, j = get_val(s, 'I'), get_val(s, 'J')
        f_val, s_val, t_val = get_val(s, 'F'), get_val(s, 'S'), get_val(s, 'T')
        if f_val is not None:
            feed = f_val
        if s_val is not None:
            spindle = s_val
        if t_val is not None:
            tool = int(t_val)
        if gz is not None:
            z = gz
        px, py = x, y
        if gx is not None:
            x = gx
        if gy is not None:
            y = gy
        has_xy = gx is not None or gy is not None
        if g == 0:
            if has_xy:
                cur = dict(start=(x, y), segs=[], z=z, feed=feed, spindle=spindle, tool=tool)
                blocks.append(cur)
            continue
        if cur is None or not has_xy or g not in (1, 2, 3):
            if cur is not None and g in (1, 2, 3) and gz is not None:
                cur['z'] = z
                if feed is not None:
                    cur['feed'] = feed
            continue
        # The block depth is the LAST Z active during cutting: a ramp entry
        # (G1 X.. Y.. Z..), a G0 Z re-approach followed by cutting, and the
        # classic G1 Z plunge all end up recorded (archive-review bug #5 —
        # ramp entries used to leave the G0 safe height in place).
        cur['z'] = z
        if feed is not None:
            cur['feed'] = feed
        if g == 1:
            if abs(x - px) < 1e-9 and abs(y - py) < 1e-9:
                continue
            cur['segs'].append(dict(type='L', p0=(px, py), p1=(x, y)))
        else:
            cur['segs'].append(dict(type='A', p0=(px, py), p1=(x, y),
                                    center=(px + (i or 0.0), py + (j or 0.0)), cw=(g == 2)))
    return blocks


def _first_loop(blk):
    """The first closed loop of a block (a contour is often cut twice, at two depths)."""
    start, out, closed = blk['start'], [], False
    for sg in blk['segs']:
        out.append(sg)
        if len(out) >= 2 and math.hypot(sg['p1'][0] - start[0], sg['p1'][1] - start[1]) < 1e-3:
            closed = True
            break
    return out, closed


def _sweep(sg):
    (x0, y0), (x1, y1), (cx, cy) = sg['p0'], sg['p1'], sg['center']
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    sw = -((a0 - a1) % TAU) if sg['cw'] else (a1 - a0) % TAU
    if abs(sw) < 1e-9:
        sw = -TAU if sg['cw'] else TAU
    return a0, sw


def _bulge(sg):
    if sg['type'] == 'L':
        return 0.0
    _, sw = _sweep(sg)
    return math.tan(sw / 4) if abs(sw) < TAU - 1e-6 else 0.0


def _sample(segs, start, step_deg=4.0):
    pts = [start]
    for sg in segs:
        if sg['type'] == 'A':
            a0, sw = _sweep(sg)
            cx, cy = sg['center']
            r = math.hypot(sg['p0'][0] - cx, sg['p0'][1] - cy)
            n = max(2, int(abs(math.degrees(sw)) / step_deg))
            for k in range(1, n):
                a = a0 + sw * k / n
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        pts.append(sg['p1'])
    return pts


def _fit_circle(pts):
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    u = [p[0] - mx for p in pts]
    v = [p[1] - my for p in pts]
    suu = sum(a * a for a in u); svv = sum(b * b for b in v)
    suv = sum(a * b for a, b in zip(u, v))
    suuu = sum(a ** 3 for a in u); svvv = sum(b ** 3 for b in v)
    suvv = sum(a * b * b for a, b in zip(u, v)); svuu = sum(b * a * a for a, b in zip(u, v))
    det = suu * svv - suv * suv
    if abs(det) < 1e-12:
        return mx, my, 0.0
    uc = (0.5 * (suuu + suvv) * svv - 0.5 * (svvv + svuu) * suv) / det
    vc = (0.5 * (svvv + svuu) * suu - 0.5 * (suuu + suvv) * suv) / det
    return mx + uc, my + vc, math.sqrt(uc * uc + vc * vc + (suu + svv) / n)


def extract_vectors(lines, tool_dia, tool_map=None):
    """Vectors rebuilt from the toolpath:
       holes     - small closed loops  -> circle (diameter = loop + tool)
       contours  - everything else     -> polyline with arc bulges
       tool_map  - optional {int parking T: float dia mm}; when the block's
                   parking has an entry, that diameter compensates the hole
                   instead of the global fallback ``tool_dia``."""
    holes, contours = [], []
    for blk in _blocks(lines):
        segs, closed = _first_loop(blk)
        if not segs:
            continue
        pts = _sample(segs, blk['start'])
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        if closed and max(xs) - min(xs) < HOLE_MAX_SIZE and max(ys) - min(ys) < HOLE_MAX_SIZE:
            cx, cy, r = _fit_circle(pts[:-1])
            path_dia = 2 * r
            t = blk.get('tool')
            comp = tool_map.get(int(t)) if (tool_map and t is not None) else None
            used = comp if comp is not None else tool_dia
            holes.append(dict(cx=round(cx, 2), cy=round(cy, 2), dia=round(path_dia + used, 1),
                              tool=(int(t) if t is not None else None),
                              path_dia=round(path_dia, 3), tool_dia_used=round(used, 2)))
        else:
            verts = [(blk['start'][0], blk['start'][1], _bulge(segs[0]))]
            for k, sg in enumerate(segs):
                nxt = _bulge(segs[k + 1]) if k + 1 < len(segs) else 0.0
                verts.append((sg['p1'][0], sg['p1'][1], nxt))
            contours.append(dict(verts=verts, closed=closed, pts=pts))
    return dict(holes=holes, contours=contours)


def vectors_for_json(vec):
    return dict(
        holes=vec['holes'],
        contours=[dict(closed=c['closed'], pts=[[round(x, 3), round(y, 3)] for x, y in c['pts']])
                  for c in vec['contours']])


# ---------------------------------------------------------------- geometry detection
def _outline_key(block):
    """Quality key for outline candidates among the large blocks: closed loops
    beat open paths (weight 2), arcs beat straight-only corners (weight 1) and
    a bigger bbox wins before falling back to the historical last-block rule."""
    pts = block['pts']
    xs = [p[2] for p in pts]
    ys = [p[3] for p in pts]
    area = (max(xs) - min(xs)) * (max(ys) - min(ys))
    closed = math.hypot(pts[-1][2] - pts[0][2], pts[-1][3] - pts[0][3]) < 1e-3
    arcs = any(p[1] in (2, 3) for p in pts)
    return (2 if closed else 0) + (1 if arcs else 0), area


def _reject_unsupported_syntax(lines):
    """Fail early on syntax that makes core's own geometry meaningless:
    incremental coordinates (G91) and inch units (G20) corrupt every parsed
    position, so preview and resize would both be silently wrong. R-format
    arcs are deliberately NOT rejected here — the Y-substitution keeps their
    geometry consistent per shift region and the dialect gate
    (controller/sheet_adapter) blocks them at machine output, matching the
    preview-first product behavior. Comments (... and after ;) are ignored."""
    for raw in lines:
        s = re.sub(r'\([^)]*\)', ' ', raw.strip())
        s = s.split(';', 1)[0]
        if not s.strip():
            continue
        if re.search(r'G91(?!\d)', s):
            raise CncError("برنامه با G91 (مختصات افزایشی) نوشته شده است؛ تغییر اندازه فقط با G90 (مطلق) پشتیبانی می‌شود")
        if re.search(r'G20(?!\d)', s):
            raise CncError("واحد فایل اینچ است (G20)؛ تغییر اندازه فقط با G21 (میلی‌متر) پشتیبانی می‌شود")


def detect_geometry(lines):
    """-> (levels, edge_lo, edge_hi) ; levels = [(y_centre, [x_centres...]), ...] sorted by Y.
       Same detection as resize_cnc_length.py."""
    x = y = 0.0
    g = None
    blocks, cur = [], None
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line or line == "%" or "G53" in line:
            continue
        mG = re.search(r'G(\d+)', line)
        if mG:
            gv = int(mG.group(1))
            if gv in (0, 1, 2, 3):
                g = gv
        gx, gy = get_val(line, 'X'), get_val(line, 'Y')
        if gx is not None: x = gx
        if gy is not None: y = gy
        if g == 0:
            if cur and cur['pts']:
                blocks.append(cur)
            cur = {'pts': []}
        if cur is None:
            cur = {'pts': []}
        if g in (0, 1, 2, 3):
            cur['pts'].append((idx, g, x, y))
    if cur and cur['pts']:
        blocks.append(cur)

    hole_levels, large_blocks = [], []
    for b in blocks:
        xs = [p[2] for p in b['pts']]; ys = [p[3] for p in b['pts']]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if 0 < w < 5 and 0 < h < 5 and len(b['pts']) > 5:
            hole_levels.append(((max(ys) + min(ys)) / 2, (max(xs) + min(xs)) / 2))
        elif w > 100 and h > 100:
            large_blocks.append(b)
    # انتخاب کانتور با امتیاز به‌جای «آخرین بلوک بزرگ»: حلقهٔ بسته (۲) و داشتن
    # قوس (۱) قوی‌ترین نشانهٔ کانتور بیرونی‌اند؛ مساحت بزرگ‌تر بعدی است و در
    # تساوی کامل، آخرین بلوک انتخاب می‌شود (رفتار تاریخی حفظ می‌شود). این‌طوری
    # بلوک raster که بعد از کانتور بیاید، کانتور را قاپ نمی‌زند.
    outline, best_key = None, None
    for b in large_blocks:
        key = _outline_key(b)
        if best_key is None or key >= best_key:
            outline, best_key = b, key
    if outline is None:
        raise CncError("کانتور اصلی قطعه در فایل پیدا نشد (فایل از همین نوع قطعه نیست؟)")

    hole_levels.sort(key=lambda t: t[0])
    levels = []
    for cy, cx in hole_levels:
        for lvl in levels:
            if abs(lvl[0] - cy) < 2:
                lvl[1].append(cx)
                break
        else:
            levels.append([cy, [cx]])
    levels.sort(key=lambda l: l[0])

    long_edges = []
    pts = outline['pts']
    for i in range(1, len(pts)):
        _, _, x0, y0 = pts[i - 1]
        _, g1, x1, y1 = pts[i]
        if g1 == 1 and abs(x1 - x0) < 1e-6 and abs(y1 - y0) > 50:
            long_edges.append((min(y0, y1), max(y0, y1)))
    if not long_edges:
        raise CncError("لبه‌های صاف بلند قطعه پیدا نشد")
    return levels, min(e[0] for e in long_edges), max(e[1] for e in long_edges)


# ---------------------------------------------------------------- distribution of the change
def make_families(n_gaps, symmetric):
    if not symmetric:
        return [[k] for k in range(n_gaps)]
    fams, seen = [], set()
    for k in range(n_gaps):
        m = n_gaps - 1 - k
        if k in seen:
            continue
        fam = [k] if k == m else [k, m]
        seen.update(fam)
        fams.append(fam)
    return fams


def make_increments(gap_len, families, enabled, mode, delta, manual):
    n = len(gap_len)
    inc = [0.0] * n
    if mode == 'manual':
        for k in range(n):
            inc[k] = float(manual[k]) if (manual and k < len(manual) and enabled[k]) else 0.0
        return inc
    fams = [f for f in families if all(enabled[k] for k in f)]
    if not fams:
        raise CncError("حداقل یک فاصله باید برای تغییر انتخاب شده باشد")
    w = [1.0] * len(fams) if mode == 'equal' else [sum(gap_len[k] for k in f) for f in fams]
    tot = sum(w)
    for f, wi in zip(fams, w):
        for k in f:
            inc[k] = delta * wi / tot / len(f)
    return inc


def make_shift(features, gap_inc):
    n = len(gap_inc)
    cuts = [(features[i] + features[i + 1]) / 2 for i in range(n)]
    cum = [0.0]
    for k in range(n):
        cum.append(cum[-1] + gap_inc[k])

    def shift_for(y):
        if y <= cuts[0]:
            return cum[0]
        if y > cuts[-1]:
            return cum[-1]
        for i in range(len(cuts) - 1):
            if cuts[i] < y <= cuts[i + 1]:
                return cum[i + 1]
        return cum[-1]
    return shift_for, cuts


def apply_resize(lines, shift_for):
    x = y = 0.0
    out, warnings = [], []
    for raw in lines:
        s = raw.strip()
        if not s or s == "%" or "G53" in s:
            out.append(raw)
            continue
        gy, gx = get_val(s, 'Y'), get_val(s, 'X')
        if gx is not None:
            x = gx
        y_before = y
        if gy is not None:
            y = gy
        if gy is None:
            out.append(raw)
            continue
        sh = shift_for(y)
        mG = re.search(r'G([123])(?!\d)', _code_only(s))
        if mG and mG.group(1) in ('2', '3') and abs(shift_for(y_before) - sh) > 1e-6:
            warnings.append(f"قوس نزدیک Y={y_before:.1f} روی مرز تغییر افتاده؛ شکل آن را بررسی کنید")
        if abs(sh) <= 1e-9:
            # zero shift: the line passes through byte-identical (archive-review
            # bug #9 — rewriting Y0 as Y0.000 made changed_lines meaningless)
            out.append(raw)
            continue
        out.append(_sub_word_outside_comments(raw, 'Y', f"Y{y + sh:.3f}"))
    return out, warnings


# ---------------------------------------------------------------- kerf-bend mode (خم‌کاری MDF)
KERF_MIN_PASS = 20.0     # mm — a horizontal cut shorter than this is not a kerf pass
KERF_MIN_PASSES = 3      # a serpentine band needs at least this many passes
KERF_PITCH_TOL = 1e-3    # mm — CAM raster pitch is exact; looser spacing is not kerf


def _detect_serpentine_blocks(lines):
    """Find transverse back-and-forth (serpentine) kerf raster blocks.

    A block qualifies when, between its plunge (G1 Z…) and its retract (G0 …),
    every move is either a wide horizontal pass (|dX| >= KERF_MIN_PASS at
    constant Y), a pure vertical transition, or a no-op word; passes alternate
    direction and consecutive pass gaps are uniform.  A vertical connector of a
    different length (e.g. a solid middle zone) splits the block into bands —
    still one serpentine block.  Blocks that do not qualify are simply not
    returned (fail-soft); kerf mode then fails loudly if nothing qualifies.

    Returns dicts in line order:
      {g0_idx, plunge_idx, retract_idx, span, passes=[(y, x_from, x_to), ...],
       first_dir, x_min, x_max, bands=[(y_hi, y_lo, pitch), ...], pitch}
    """
    parsed = []
    x = y = 0.0
    g = None
    for raw in lines:
        s = raw.strip()
        if not s or s == '%' or 'G53' in s:
            parsed.append(None)
            continue
        for gm in re.finditer(r'G(\d+)', s):
            gv = int(gm.group(1))
            if gv in (0, 1, 2, 3):
                g = gv
        px, py = x, y
        gx, gy, gz = get_val(s, 'X'), get_val(s, 'Y'), get_val(s, 'Z')
        if gx is not None:
            x = gx
        if gy is not None:
            y = gy
        parsed.append((g, px, py, x, y, gz))

    def _split_bands(pl):
        """pass Y list -> [(y_hi, y_lo, pitch), ...] or None when not uniform."""
        bands_, pitch_ = [], None
        cur = [pl[0]]
        for prev, nxt in zip(pl, pl[1:]):
            d = abs(nxt[0] - prev[0])
            if pitch_ is None:
                pitch_ = d
            if abs(d - pitch_) > KERF_PITCH_TOL:
                if len(cur) < KERF_MIN_PASSES:
                    return None
                bands_.append((cur[0][0], cur[-1][0], pitch_))
                cur, pitch_ = [nxt], None
            else:
                cur.append(nxt)
        if len(cur) < KERF_MIN_PASSES:
            return None
        bands_.append((cur[0][0], cur[-1][0], pitch_))
        return bands_

    blocks = []
    i, n = 0, len(parsed)
    while i < n:
        ev = parsed[i]
        if ev is None or ev[0] != 0 or (ev[3] == ev[1] and ev[4] == ev[2]):
            i += 1
            continue
        # candidate block start: a G0 with an XY move
        g0_idx = i
        j, plunge_idx, restart = i + 1, None, None
        while j < n:
            ev2 = parsed[j]
            if ev2 is None:
                j += 1
                continue
            if ev2[0] == 1 and ev2[5] is not None:
                plunge_idx = j
                break
            if ev2[0] == 0:
                if ev2[5] is None and ev2[3] == ev2[1] and ev2[4] == ev2[2]:
                    j += 1
                    continue
                restart = j
                break
            if ev2[0] in (2, 3):
                break
            j += 1
        if restart is not None:
            i = restart
            continue
        if plunge_idx is None:
            i += 1
            continue
        moves, k, retract_idx, bad = [], plunge_idx + 1, None, False
        while k < n:
            ev3 = parsed[k]
            if ev3 is None:
                k += 1
                continue
            if ev3[0] == 0:
                retract_idx = k
                break
            if ev3[0] != 1:
                bad = True
                break
            dx, dy = ev3[3] - ev3[1], ev3[4] - ev3[2]
            if abs(dx) < 1e-9 and abs(dy) < 1e-9:
                k += 1
                continue
            if abs(dy) < 1e-9 and abs(dx) >= KERF_MIN_PASS:
                moves.append(('P', ev3[4], ev3[1], ev3[3]))
            elif abs(dx) < 1e-9:
                moves.append(('T', ev3[4], None, None))
            else:
                bad = True
                break
            k += 1
        if retract_idx is None or bad or not moves:
            i = (retract_idx + 1) if retract_idx is not None else (k + 1)
            continue
        passes = []
        for mv in moves:
            if mv[0] != 'P':
                continue
            if passes and abs(passes[-1][0] - mv[1]) < 1e-9:
                passes[-1][2] = mv[3]        # split pass continuation
            else:
                passes.append([mv[1], mv[2], mv[3]])
        ok = len(passes) >= KERF_MIN_PASSES
        dirs = []
        if ok:
            for p_ in passes:
                if abs(p_[2] - p_[1]) < 1e-9:
                    ok = False
                    break
                dirs.append(1 if p_[2] > p_[1] else -1)
        if ok:
            for a, b_ in zip(dirs, dirs[1:]):
                if a == b_:
                    ok = False
                    break
        bands_ = _split_bands(passes) if ok else None
        if ok and bands_:
            blocks.append(dict(
                g0_idx=g0_idx, plunge_idx=plunge_idx, retract_idx=retract_idx,
                span=(g0_idx, retract_idx), passes=passes, first_dir=dirs[0],
                x_min=min(min(p_[1], p_[2]) for p_ in passes),
                x_max=max(max(p_[1], p_[2]) for p_ in passes),
                bands=bands_, pitch=bands_[0][2],
            ))
        i = retract_idx + 1 if retract_idx != g0_idx else i + 1
    return blocks


def _process_kerf(text, lines, eol, delta, tool_dia, params):
    """mode='kerf': regenerate the transverse serpentine field so the pitch stays
    exactly uniform, the previously solid middle zone gets grooves too, and the
    extra length is realised as NEW passes — never by stretching the pitch.
    The remainder (delta mod pitch) grows the zone below the field."""
    tool_map = _tool_map_from(params)
    tool_warn = _tool_warnings(lines, tool_map, tool_dia)
    try:
        levels, edge_lo, edge_hi = detect_geometry(lines)
    except CncError as exc:
        if params.get('allow_view_only'):
            return view_only_result(text, lines, eol, tool_dia, str(exc),
                                    extra_warnings=tool_warn, tool_map=tool_map)
        raise
    blocks = _detect_serpentine_blocks(lines)
    if not blocks:
        raise CncError("مود kerf: هیچ نوار شیار رفت‌وبرگشتی (سرپنتاین) در فایل پیدا نشد؛ "
                       "برای این فایل از روش‌های معمولی استفاده کنید")
    pitch = blocks[0]['pitch']
    all_bands = [band for b in blocks for band in b['bands']]
    if any(abs(band[2] - pitch) > KERF_PITCH_TOL for band in all_bands):
        raise CncError("مود kerf: گام شیارها در نوارهای فایل یکی نیست؛ بازتولید امن نیست")
    base_ys = sorted(round(p_[0], 6) for p_ in blocks[0]['passes'])
    for b in blocks[1:]:
        ys = sorted(round(p_[0], 6) for p_ in b['passes'])
        if len(ys) != len(base_ys) or any(abs(a - c) > 1e-6 for a, c in zip(base_ys, ys)):
            raise CncError("مود kerf: موقعیت شیارها بین لایه‌های عمق هم‌راست نیست؛ بازتولید امن نیست")
    F0 = min(p_[0] for b in blocks for p_ in b['passes'])
    F1 = max(p_[0] for b in blocks for p_ in b['passes'])
    for cy, _xs in levels:
        if F0 + 1e-6 < cy < F1 - 1e-6:
            raise CncError(f"مود kerf: سوراخ داخل ناحیهٔ شیار است (Y={cy:.3f})؛ "
                           "الگوی خم‌کاری با سوراخ داخل ناحیهٔ خم سازگار نیست")
    serp_idx = set()
    for b in blocks:
        serp_idx.update(range(b['span'][0], b['span'][1] + 1))

    def _modal(s_, g_, x_, y_):
        for gm in re.finditer(r'G(\d+)', s_):
            gv = int(gm.group(1))
            if gv in (0, 1, 2, 3):
                g_ = gv
        gx_, gy_ = get_val(s_, 'X'), get_val(s_, 'Y')
        if gx_ is not None:
            x_ = gx_
        if gy_ is not None:
            y_ = gy_
        return g_, x_, y_

    # any other motion vertex strictly inside the field breaks regeneration
    x = y = 0.0
    g = None
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if not s or s == '%' or 'G53' in s:
            continue
        g, x, y = _modal(s, g, x, y)
        if idx in serp_idx:
            continue
        if get_val(s, 'X') is None and get_val(s, 'Y') is None:
            continue
        if g in (0, 1, 2, 3) and F0 + 1e-6 < y < F1 - 1e-6:
            raise CncError(f"مود kerf: هندسهٔ دیگری داخل ناحیهٔ شیار است (Y={y:.3f})؛ بازتولید امن نیست")

    S = F1 - F0
    m = math.floor(delta / pitch + 1e-9)
    S_new = S + m * pitch
    if S_new < pitch:
        raise CncError("مود kerf: با این کاهش طول، ناحیهٔ شیار از بین می‌رود؛ کاهش را کمتر کنید")
    r = round(delta - m * pitch, 6)
    K = int(math.floor(S_new / pitch + 1e-9))
    if K < 1:
        raise CncError("مود kerf: ناحیهٔ شیار پس از این تغییر خیلی کوتاه است")
    F0_new = F0 + r
    F1_new = F0_new + S_new

    def shift_for(y_):
        return 0.0 if y_ <= F0 + 1e-6 else delta

    # regenerate every serpentine block: same corner, same X span, uniform pitch
    gen, gen_end, span_end = {}, {}, {}
    for b in blocks:
        xs0, xs1, d0 = b['x_min'], b['x_max'], b['first_dir']
        out_b = [re.sub(r'Y' + NUM, f"Y{F1_new:.3f}", lines[b['g0_idx']], count=1),
                 lines[b['plunge_idx']]]
        xt = yk = None
        for k in range(K + 1):
            yk = F1_new - k * pitch
            xt = (xs1 if d0 > 0 else xs0) if k % 2 == 0 else (xs0 if d0 > 0 else xs1)
            if k == 0:
                out_b.append(f"G1X{xt:.3f}")
            else:
                out_b.append(f"Y{yk:.3f}")
                out_b.append(f"X{xt:.3f}")
        out_b.append(lines[b['retract_idx']])
        gen[b['g0_idx']] = out_b
        gen_end[b['g0_idx']] = (xt, yk)
        span_end[b['g0_idx']] = b['span'][1]

    out, warnings = [], []
    x = y = 0.0
    idx, n = 0, len(lines)
    while idx < n:
        if idx in gen:
            out.extend(gen[idx])
            x, y = gen_end[idx]
            idx = span_end[idx] + 1
            continue
        raw = lines[idx]
        s = raw.strip()
        if not s or s == '%' or 'G53' in s:
            out.append(raw)
            idx += 1
            continue
        gy, gx = get_val(s, 'Y'), get_val(s, 'X')
        if gx is not None:
            x = gx
        y_before = y
        if gy is not None:
            y = gy
        if gy is None:
            out.append(raw)
            idx += 1
            continue
        sh = shift_for(y)
        mG = re.search(r'G([123])(?!\d)', s)
        if mG and mG.group(1) in ('2', '3') and abs(shift_for(y_before) - sh) > 1e-6:
            warnings.append(f"قوس نزدیک Y={y_before:.1f} روی مرز ناحیهٔ شیار افتاده؛ شکل آن را بررسی کنید")
        out.append(re.sub(r'Y' + NUM, f"Y{y + sh:.3f}", raw, count=1))
        idx += 1

    # uniform N numbering for the regenerated program (N10, N20, …)
    res, nv = [], 0
    for raw in out:
        s = raw.strip()
        if not s or s == '%':
            res.append(raw)
            continue
        nv += 10
        res.append(re.sub(r'^N\d+', f'N{nv}', raw, count=1) if re.match(r'^N\d+', s)
                   else f"N{nv}{s}")
    out = res

    v0 = extract_vectors(lines, tool_dia, tool_map)
    v1 = extract_vectors(out, tool_dia, tool_map)
    a0, b0 = _span(v0)
    a1, b1 = _span(v1)
    if abs((b1 - a1) - (b0 - a0) - delta) > 0.05:
        raise CncError(f"مود kerf: اعتبارسنجی طول شکست خورد "
                       f"(Δ={b1 - a1 - (b0 - a0):.3f} در برابر {delta:.3f})")
    blocks2 = _detect_serpentine_blocks(out)
    if len(blocks2) != len(blocks):
        raise CncError("مود kerf: اعتبارسنجی ساختار شکست خورد (تعداد بلوک‌های شیار تغییر کرد)")
    for b2 in blocks2:
        if len(b2['bands']) != 1:
            raise CncError("مود kerf: اعتبارسنجی پیوستگی شکست خورد (ناحیهٔ خالی باقی مانده است)")
        if len(b2['passes']) != K + 1:
            raise CncError("مود kerf: اعتبارسنجی تعداد شیار شکست خورد")
    passes_before = len(blocks[0]['passes'])
    warnings += tool_warn
    warnings.insert(0, (
        f"مود kerf: گام {pitch:.3f} میلی‌متر ثابت ماند و ناحیهٔ خالی وسط با شیار پر شد؛ "
        f"تعداد شیار در هر عمق: {passes_before} ← {K + 1}؛ "
        f"باقیماندهٔ {r:.3f} میلی‌متر به ناحیهٔ پایین افزوده شد"
    ))
    changed = [i for i, (p_, q_) in enumerate(zip(lines, out)) if p_ != q_]
    return dict(
        ok=True, resizable=True, kerf=dict(
            pitch=round(pitch, 3), passes_before=passes_before, passes_after=K + 1,
            remainder=round(r, 3), field=(round(F0_new, 3), round(F1_new, 3)),
        ),
        features=[dict(y=round(f, 3)) for f in [edge_lo] + [l[0] for l in levels] + [edge_hi]],
        hole_rows=[dict(y=round(l[0], 3), xs=[round(xx, 2) for xx in l[1]]) for l in levels],
        gaps=[], families=[], cuts=[],
        delta=round(delta, 3),
        length_before=round(b0 - a0, 3), length_after=round(b1 - a1, 3),
        finished_length_before=round(b0 - a0 + _finished_tool_dia(lines, tool_map, tool_dia)[0], 3),
        finished_length_after=round(b1 - a1 + _finished_tool_dia(lines, tool_map, tool_dia)[0], 3),
        finished_tool_dia=round(_finished_tool_dia(lines, tool_map, tool_dia)[0], 3),
        original=vectors_for_json(v0), resized=vectors_for_json(v1),
        warnings=warnings, changed_lines=changed,
        cnc_text=eol.join(out) + eol, dxf_text=to_dxf(v1),
    )


# ---------------------------------------------------------------- DXF
def to_dxf(vec):
    """R12 ASCII DXF: layer OUTLINE (polylines with bulge), layer HOLES (final
    compensated circles) and layer TOOLPATH (raw centerline circles, so the
    operator can always see what the G-code really contains)."""
    out = []
    w = lambda code, val: out.extend([str(code), str(val)])
    w(0, "SECTION"); w(2, "ENTITIES")
    for h in vec['holes']:
        pd = h.get('path_dia')
        if pd:
            w(0, "CIRCLE"); w(8, "TOOLPATH")
            w(10, f"{h['cx']:.4f}"); w(20, f"{h['cy']:.4f}"); w(30, "0.0"); w(40, f"{pd / 2:.4f}")
    for h in vec['holes']:
        w(0, "CIRCLE"); w(8, "HOLES")
        w(10, f"{h['cx']:.4f}"); w(20, f"{h['cy']:.4f}"); w(30, "0.0"); w(40, f"{h['dia'] / 2:.4f}")
    for c in vec['contours']:
        verts = c['verts'][:-1] if c['closed'] else c['verts']
        w(0, "POLYLINE"); w(8, "OUTLINE"); w(66, 1); w(70, 1 if c['closed'] else 0)
        w(10, "0.0"); w(20, "0.0"); w(30, "0.0")
        for x, y, b in verts:
            w(0, "VERTEX"); w(8, "OUTLINE")
            w(10, f"{x:.4f}"); w(20, f"{y:.4f}"); w(30, "0.0")
            if abs(b) > 1e-12:
                w(42, f"{b:.8f}")
        w(0, "SEQEND"); w(8, "OUTLINE")
    w(0, "ENDSEC"); w(0, "EOF")
    return "\r\n".join(out) + "\r\n"


# ---------------------------------------------------------------- main entry
def _num(params, key, default, lo, hi):
    v = params.get(key, default)
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise CncError(f"مقدار «{key}» عدد معتبر نیست")
    if not (lo <= v <= hi) or v != v:
        raise CncError(f"مقدار «{key}» باید بین {lo} و {hi} باشد")
    return v


def _span(vec):
    ys = [p[1] for c in vec['contours'] for p in c['pts']]
    return (min(ys), max(ys)) if ys else (0.0, 0.0)


# ------------------------------------------------------- tool table support
def _tool_map_from(params):
    """Lenient {parking: diameter} extraction — the map comes from the operator's
    tool table; junk entries are dropped instead of blocking a display feature."""
    raw = params.get('tool_map')
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                sk, fv = int(k), float(v)
            except (TypeError, ValueError):
                continue
            if 1 <= sk <= 10 and 0.1 <= fv <= 100 and fv == fv:
                out[sk] = fv
    return out


def _tools_in_file(lines):
    tools = set()
    for l in lines:
        tv = get_val(l, 'T')
        if tv is None:
            continue
        try:
            ti = int(tv)
        except (TypeError, ValueError):
            continue
        if 0 <= ti <= 99:
            tools.add(ti)
    return tools


def _tool_warnings(lines, tool_map, tool_dia):
    """Make the diameter assumption VISIBLE: which parkings were used from the
    tool table and which fell back to the default diameter."""
    tools = _tools_in_file(lines)
    if not tools:
        return []
    ws = []
    known = sorted(t for t in tools if t in tool_map)
    missing = sorted(t for t in tools if t not in tool_map)
    if known:
        ws.append("میز ابزار اعمال شد: " + "، ".join(f"T{t} = {tool_map[t]:g}mm" for t in known) + ".")
    if missing:
        ws.append("پارکینگ " + "، ".join(f"T{t}" for t in missing) +
                  f" در میز ابزار ثبت نشده؛ جبران سوراخ‌ها با قطر پیش‌فرض {tool_dia:g}mm انجام شد.")
    return ws


def _finished_tool_dia(lines, tool_map, fallback):
    """Diameter used for finished-size reporting (archive-review bug #8): the
    mapped diameter of the file's tool(s) from the operator's tool table, else
    the request fallback. Returns ``(dia, from_table, ambiguous)``."""
    tools = _tools_in_file(lines)
    dias = sorted({round(float(tool_map[t]), 4) for t in tools if t in tool_map})
    if len(dias) == 1:
        return dias[0], True, False
    if len(dias) > 1:
        return dias[0], True, True
    return float(fallback), False, False


def view_only_result(text, lines, eol, tool_dia, reason, extra_warnings=(), tool_map=None):
    """Return geometry for viewing without permitting an automatic resize."""
    vectors = extract_vectors(lines, tool_dia)
    a, b = _span(vectors)
    fin_dia, _, _ = _finished_tool_dia(lines, tool_map or {}, tool_dia)
    shown = vectors_for_json(vectors)
    return dict(
        ok=True, resizable=False, features=[], hole_rows=[], gaps=[], families=[], cuts=[],
        delta=0.0, length_before=round(b - a, 3), length_after=round(b - a, 3),
        finished_length_before=round(b - a + fin_dia, 3),
        finished_length_after=round(b - a + fin_dia, 3),
        finished_tool_dia=round(fin_dia, 3),
        original=shown, resized=shown,
        warnings=[f"این فایل فقط نمایش داده می‌شود و قابل تغییر طول نیست: {reason}"] + list(extra_warnings),
        changed_lines=[], cnc_text=text, dxf_text=to_dxf(vectors),
    )


def process(text, params=None):
    """params: mode ('equal'|'proportional'|'manual'), delta (mm), symmetric (bool),
               enabled ([bool per gap]), manual ([mm per gap]), tool_dia (mm).
       Returns a JSON-serialisable dict, or raises CncError."""
    params = params or {}
    if not isinstance(text, str) or not text.strip():
        raise CncError("فایل خالی است")
    if len(text) > MAX_BYTES:
        raise CncError("فایل بیش از حد بزرگ است")
    lines, eol = split_lines(text)
    if not any(re.search(r'G[0-3](?!\d)', l) for l in lines):
        raise CncError("این فایل شبیه G-code نیست")
    _reject_unsupported_syntax(lines)

    tool_map = _tool_map_from(params)
    try:
        _td_fallback = float(params.get('tool_dia', 8.0))
    except (TypeError, ValueError):
        _td_fallback = 8.0
    tool_warn = _tool_warnings(lines, tool_map, _td_fallback)

    profile = params.get('profile') or params.get('feature_profile')
    if profile is not None:
        inventory = analyze_program(lines)
        errors = validate_profile(profile, inventory)
        vectors = extract_vectors(lines, float(params.get('tool_dia', 8.0)), tool_map)
        shown = vectors_for_json(vectors)
        a, b = _span(vectors)
        if errors:
            return dict(ok=True, resizable=False, profile_mode=True, profile_errors=errors,
                        features=inventory, original=shown, resized=shown,
                        length_before=round(b-a,3), length_after=round(b-a,3), delta=0.0,
                        warnings=['این فایل فقط نمایش داده می‌شود تا همهٔ featureها تأیید شوند.'] + tool_warn,
                        changed_lines=[], cnc_text=text, dxf_text=to_dxf(vectors))
        # Safe default: approved profiles still need explicit legacy opt-in until
        # segment-level stretch rules are saved; this prevents arc distortion.
        if not profile.get('legacy_gap_resize'):
            return dict(ok=True, resizable=False, profile_mode=True, profile_errors=[
                        'ناحیهٔ خطوط صاف قابل تغییر هنوز در پروفایل ثبت نشده است'],
                        features=inventory, original=shown, resized=shown,
                        length_before=round(b-a,3), length_after=round(b-a,3), delta=0.0,
                        warnings=['برای فعال‌سازی تولید، نقاط شروع و پایان ناحیهٔ کشش را ثبت کنید.'] + tool_warn,
                        changed_lines=[], cnc_text=text, dxf_text=to_dxf(vectors))
        legacy = dict(params)
        legacy.pop('profile', None); legacy.pop('feature_profile', None)
        result = process(text, legacy)
        result.update(profile_mode=True, features=inventory, profile_errors=[])
        result['warnings'].append('پروفایل اجرا شد؛ Z/Feed/Spindle و شعاع منحنی‌ها تغییر داده نشدند.')
        return result

    mode = params.get('mode', 'equal')
    if mode not in ('equal', 'proportional', 'manual', 'kerf'):
        raise CncError("روش توزیع نامعتبر است")
    delta = _num(params, 'delta', 0.0, -5000, 5000)
    tool_dia = _num(params, 'tool_dia', 8.0, 0.1, 100)
    symmetric = bool(params.get('symmetric', True))

    if mode == 'kerf':
        return _process_kerf(text, lines, eol, delta, tool_dia, params)

    try:
        levels, edge_lo, edge_hi = detect_geometry(lines)
    except CncError as exc:
        if params.get('allow_view_only'):
            return view_only_result(text, lines, eol, tool_dia, str(exc),
                                    extra_warnings=tool_warn, tool_map=tool_map)
        raise
    mids = [l for l in levels if edge_lo < l[0] < edge_hi]
    features = [edge_lo] + [l[0] for l in mids] + [edge_hi]
    n = len(features) - 1
    gap_len = [features[k + 1] - features[k] for k in range(n)]
    families = make_families(n, symmetric)

    enabled = params.get('enabled')
    enabled = [bool(e) for e in enabled] if isinstance(enabled, list) and len(enabled) == n else [True] * n
    manual = params.get('manual') if isinstance(params.get('manual'), list) else None

    inc = make_increments(gap_len, families, enabled, mode, delta, manual)
    total = sum(inc)
    shift_for, cuts = make_shift(features, inc)

    warnings = []
    for k in range(n):
        if gap_len[k] + inc[k] < 1.0:
            raise CncError(f"فاصله‌ی شماره‌ی {k + 1} با این مقدار منفی/صفر می‌شود؛ تغییر را کمتر کنید")
        if inc[k] < 0 and gap_len[k] + inc[k] < 15:
            warnings.append(f"فاصله‌ی شماره‌ی {k + 1} فقط {gap_len[k] + inc[k]:.1f} میلی‌متر می‌ماند؛ "
                            f"سوراخ‌ها به هم نزدیک می‌شوند")

    out_lines, w2 = apply_resize(lines, shift_for)
    warnings += w2
    warnings += tool_warn

    v0 = extract_vectors(lines, tool_dia, tool_map)
    v1 = extract_vectors(out_lines, tool_dia, tool_map)
    a0, b0 = _span(v0)
    a1, b1 = _span(v1)

    changed = [i for i, (p, q) in enumerate(zip(lines, out_lines)) if p != q]
    fin_dia, fin_from_table, fin_ambiguous = _finished_tool_dia(lines, tool_map, tool_dia)
    if fin_ambiguous:
        warnings.append("چند قطر ابزار مختلف در فایل هست؛ جبران اندازهٔ نهایی با کوچک‌ترین قطر ثبت‌شده انجام شد.")
    warnings.append(
        f"طول نهایی قطعه (finished) = مسیر مرکز ابزار + قطر ابزار برش ({fin_dia:g}mm) گزارش شد؛ "
        "«طول قبل/بعد» همچنان مسیر مرکز ابزار است و هدف طول را بر اساس finished_length وارد کنید.")
    return dict(
        ok=True, resizable=True,
        features=[dict(y=round(f, 3)) for f in features],
        hole_rows=[dict(y=round(l[0], 3), xs=[round(x, 2) for x in l[1]]) for l in levels],
        gaps=[dict(index=k, y0=round(features[k], 3), y1=round(features[k + 1], 3),
                   length=round(gap_len[k], 3), increment=round(inc[k], 3),
                   new_length=round(gap_len[k] + inc[k], 3), enabled=enabled[k])
              for k in range(n)],
        families=families,
        cuts=[round(c, 3) for c in cuts],
        delta=round(total, 3),
        length_before=round(b0 - a0, 3),
        length_after=round(b1 - a1, 3),
        finished_length_before=round(b0 - a0 + fin_dia, 3),
        finished_length_after=round(b1 - a1 + fin_dia, 3),
        finished_tool_dia=round(fin_dia, 3),
        original=vectors_for_json(v0),
        resized=vectors_for_json(v1),
        warnings=warnings,
        changed_lines=changed,
        cnc_text=eol.join(out_lines) + eol,
        dxf_text=to_dxf(v1),
    )


def handle_payload(payload):
    """Framework-independent API handler for an already-parsed JSON payload
    ({filename, content, params}).  Returns (http_status, dict).  Lets HTTP
    views parse the request body exactly once."""
    try:
        text, params = payload['content'], payload.get('params') or {}
        if not isinstance(params, dict):
            raise ValueError
        action = payload.get('action')
    except Exception:
        return 400, dict(ok=False, error="درخواست نامعتبر است")
    try:
        if action == 'analyze':
            return 200, analyze(text, params)
        return 200, process(text, {**params, 'allow_view_only': True})
    except CncError as e:
        return 400, dict(ok=False, error=str(e))
    except Exception:
        log.exception("cnc_resizer: unexpected error")
        return 500, dict(ok=False, error="خطای غیرمنتظره در پردازش فایل")


def handle_request(raw):
    """Parse the raw request body (bytes) and delegate to handle_payload."""
    try:
        payload = json.loads(raw.decode('utf-8'))
    except Exception:
        return 400, dict(ok=False, error="درخواست نامعتبر است")
    return handle_payload(payload)

# --- Generic operator-approved feature profiles ----------------------------
def program_summary(lines):
    """File-level G-code feature summary for the operator UI (تحلیل ویژگی G-code)."""
    g_codes, m_codes, tools, feeds, spindles = set(), set(), set(), set(), set()
    units, wcs, arc_format, n_step = None, set(), None, None
    blocks = _blocks(lines)
    n_values = []
    move_count = 0
    arc_count = 0
    for raw in lines:
        s = raw.strip()
        if not s or s == "%":
            continue
        nm = re.match(r'^N(\d+)', s)
        if nm:
            n_values.append(int(nm.group(1)))
        for gm in re.finditer(r'G(\d+)(?!\d)', s):
            g_codes.add(int(gm.group(1)))
        for mm_ in re.finditer(r'M(\d+)(?!\d)', s):
            m_codes.add(int(mm_.group(1)))
        tv = get_val(s, 'T')
        if tv is not None:
            tools.add(int(tv))
        fv, sv = get_val(s, 'F'), get_val(s, 'S')
        if fv is not None:
            feeds.add(fv)
        if sv is not None:
            spindles.add(sv)
        if re.search(r'G20(?!\d)', s):
            units = 'G20'
        if re.search(r'G21(?!\d)', s):
            units = 'G21'
        wm = re.search(r'G5([4-9])(?!\d)', s)
        if wm:
            wcs.add('G5' + wm.group(1))
        if re.search(r'\bG[23][^\d]*(?:[IJK])', s) or (re.search(r'G[23](?!\d)', s) and re.search(r'[IJ]\s*' + NUM, s)):
            arc_format = arc_format or 'IJ'
        if re.search(r'G[23](?!\d)[^\n]*R\s*' + NUM, s):
            arc_format = 'R'
    if n_values and len(n_values) > 1:
        steps = {b - a for a, b in zip(n_values, n_values[1:]) if b > a}
        if len(steps) == 1:
            n_step = steps.pop()
    for blk in blocks:
        move_count += len(blk['segs'])
        arc_count += sum(sg['type'] == 'A' for sg in blk['segs'])
    depths = sorted({round(blk['z'], 3) for blk in blocks if blk.get('z') is not None and blk['z'] < 0})
    return {
        'line_count': len([l for l in lines if l.strip()]),
        'blocks': len(blocks),
        'moves': move_count,
        'arcs': arc_count,
        'g_codes': sorted(g_codes),
        'm_codes': sorted(m_codes),
        'tools': sorted(tools),
        'feeds': sorted(feeds),
        'spindles': sorted(spindles),
        'units': units,
        'work_offsets': sorted(wcs),
        'arc_format': arc_format,
        'n_numbering_step': n_step,
        'depth_levels': depths,
    }


def analyze_program(lines):
    """Build a conservative geometry inventory; tool numbers are metadata only."""
    out = []
    for idx, block in enumerate(_blocks(lines), 1):
        segs = block.get('segs', [])
        if not segs:
            continue
        pts = _sample(segs, block['start'])
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        w, h = max(xs)-min(xs), max(ys)-min(ys)
        closed = math.hypot(pts[-1][0]-pts[0][0], pts[-1][1]-pts[0][1]) < 1e-3
        arcs = sum(s['type'] == 'A' for s in segs)
        if closed and w <= HOLE_MAX_SIZE and h <= HOLE_MAX_SIZE:
            kind = 'hole'
        elif closed and arcs and w > 100 and h > 100:
            kind = 'outer_contour'
        elif w > 100 and h > 100:
            kind = 'raster_or_area'
        elif max(w, h) > 50:
            kind = 'linear_groove'
        else:
            kind = 'unknown'
        out.append({'id': f'feature-{idx}', 'kind': kind,
                    'bounds': {'x0': round(min(xs),3), 'y0': round(min(ys),3),
                               'x1': round(max(xs),3), 'y1': round(max(ys),3)},
                    'closed': closed, 'segments': len(segs), 'arcs': arcs,
                    'depth': block.get('z'),
                    'feed': block.get('feed'),
                    'spindle': block.get('spindle'),
                    'tool': block.get('tool'),
                    'start': [round(v,3) for v in block['start']]})
    return out


def validate_profile(profile, inventory):
    if not isinstance(profile, dict):
        return ['پروفایل هندسی وجود ندارد']
    errors = []
    if profile.get('axis', 'length') != 'length':
        errors.append('محور تغییر CNC باید length باشد؛ طول روی Y قرار دارد')
    rules = profile.get('features') or {}
    if not isinstance(rules, dict):
        errors.append('قواعد featureها باید یک شیء معتبر باشند')
        rules = {}
    known = {f['id'] for f in inventory}
    for fid in rules:
        if fid not in known:
            errors.append(f'feature ناشناخته در پروفایل: {fid}')
    for item in inventory:
        rule = rules.get(item['id'])
        mode = rule.get('mode') if isinstance(rule, dict) else None
        if not rule or mode in (None, 'view_only', 'unknown'):
            errors.append(f"feature {item['id']} هنوز توسط اپراتور تأیید نشده است")
        elif mode not in ('fixed', 'stretch'):
            errors.append(f"حالت feature {item['id']} پشتیبانی نمی‌شود")
        if item['kind'] in ('outer_contour', 'hole') and mode == 'stretch':
            errors.append(f"تغییر مستقیم {item['kind']} مجاز نیست؛ فقط خطوط صاف میانی را انتخاب کنید")
    reference_points = profile.get('reference_points')
    if not isinstance(reference_points, dict) or not reference_points:
        errors.append('حداقل یک نقطهٔ مرجع اپراتور ثبت نشده است')
    else:
        try:
            values = [float(value) for value in reference_points.values()]
            if not values or not all(math.isfinite(value) for value in values):
                raise ValueError
            if 'start' in reference_points and 'end' in reference_points and float(reference_points['start']) == float(reference_points['end']):
                errors.append('نقاط مرجع شروع و پایان باید متفاوت باشند')
        except (TypeError, ValueError, OverflowError):
            errors.append('مقادیر نقاط مرجع باید عددی و متناهی باشند')
    return errors


def analyze(text, params=None):
    params = params or {}
    if not isinstance(text, str) or not text.strip():
        raise CncError('فایل خالی است')
    if len(text) > MAX_BYTES:
        raise CncError('فایل بیش از حد بزرگ است')
    lines, _ = split_lines(text)
    if not any(re.search(r'G[0-3](?!\d)', l) for l in lines):
        raise CncError('این فایل شبیه G-code نیست')
    _reject_unsupported_syntax(lines)
    inventory = analyze_program(lines)
    summary = program_summary(lines)
    profile = params.get('profile') or params.get('feature_profile')
    errors = validate_profile(profile, inventory) if profile else []
    return {'ok': True, 'features': inventory, 'summary': summary,
            'profile_errors': errors,
            'ready': bool(profile and not errors), 'warnings': []}
