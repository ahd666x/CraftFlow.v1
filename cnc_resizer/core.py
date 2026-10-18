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


class CncError(Exception):
    """Problem with the input the user should be told about (message is Persian)."""


# ---------------------------------------------------------------- parsing
def split_lines(text):
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines, eol


def get_val(line, letter):
    m = re.search(letter + '(' + NUM + ')', line)
    return float(m.group(1)) if m else None


def _blocks(lines):
    """Split the program into blocks started by a rapid (G0) XY move.
    Each block: dict(start=(x,y), segs=[dict(type, p0, p1, center, cw)])."""
    x = y = 0.0
    g = None
    blocks, cur = [], None
    for raw in lines:
        s = raw.strip()
        if not s or s == "%" or "G53" in s:
            continue
        for gm in re.finditer(r'G(\d+)', s):
            gv = int(gm.group(1))
            if gv in (0, 1, 2, 3):
                g = gv
        gx, gy = get_val(s, 'X'), get_val(s, 'Y')
        i, j = get_val(s, 'I'), get_val(s, 'J')
        px, py = x, y
        if gx is not None:
            x = gx
        if gy is not None:
            y = gy
        has_xy = gx is not None or gy is not None
        if g == 0:
            if has_xy:
                cur = dict(start=(x, y), segs=[])
                blocks.append(cur)
            continue
        if cur is None or not has_xy or g not in (1, 2, 3):
            continue
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


def extract_vectors(lines, tool_dia):
    """Vectors rebuilt from the toolpath:
       holes     - small closed loops  -> circle (diameter = loop + tool)
       contours  - everything else     -> polyline with arc bulges"""
    holes, contours = [], []
    for blk in _blocks(lines):
        segs, closed = _first_loop(blk)
        if not segs:
            continue
        pts = _sample(segs, blk['start'])
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        if closed and max(xs) - min(xs) < HOLE_MAX_SIZE and max(ys) - min(ys) < HOLE_MAX_SIZE:
            cx, cy, r = _fit_circle(pts[:-1])
            dia = round(2 * r + tool_dia, 1)
            holes.append(dict(cx=round(cx, 2), cy=round(cy, 2), dia=dia))
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

    hole_levels, outline = [], None
    for b in blocks:
        xs = [p[2] for p in b['pts']]; ys = [p[3] for p in b['pts']]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if 0 < w < 5 and 0 < h < 5 and len(b['pts']) > 5:
            hole_levels.append(((max(ys) + min(ys)) / 2, (max(xs) + min(xs)) / 2))
        elif w > 100 and h > 100:
            outline = b
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
        mG = re.search(r'G([123])(?!\d)', s)
        if mG and mG.group(1) in ('2', '3') and abs(shift_for(y_before) - sh) > 1e-6:
            warnings.append(f"قوس نزدیک Y={y_before:.1f} روی مرز تغییر افتاده؛ شکل آن را بررسی کنید")
        out.append(re.sub(r'Y' + NUM, f"Y{y + sh:.3f}", raw, count=1))
    return out, warnings


# ---------------------------------------------------------------- DXF
def to_dxf(vec):
    """R12 ASCII DXF: layer OUTLINE (polylines with bulge) and layer HOLES (circles)."""
    out = []
    w = lambda code, val: out.extend([str(code), str(val)])
    w(0, "SECTION"); w(2, "ENTITIES")
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


def view_only_result(text, lines, eol, tool_dia, reason):
    """Return geometry for viewing without permitting an automatic resize."""
    vectors = extract_vectors(lines, tool_dia)
    a, b = _span(vectors)
    shown = vectors_for_json(vectors)
    return dict(
        ok=True, resizable=False, features=[], hole_rows=[], gaps=[], families=[], cuts=[],
        delta=0.0, length_before=round(b - a, 3), length_after=round(b - a, 3),
        original=shown, resized=shown,
        warnings=[f"این فایل فقط نمایش داده می‌شود و قابل تغییر طول نیست: {reason}"],
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

    profile = params.get('profile') or params.get('feature_profile')
    if profile is not None:
        inventory = analyze_program(lines)
        errors = validate_profile(profile, inventory)
        vectors = extract_vectors(lines, float(params.get('tool_dia', 8.0)))
        shown = vectors_for_json(vectors)
        a, b = _span(vectors)
        if errors:
            return dict(ok=True, resizable=False, profile_mode=True, profile_errors=errors,
                        features=inventory, original=shown, resized=shown,
                        length_before=round(b-a,3), length_after=round(b-a,3), delta=0.0,
                        warnings=['این فایل فقط نمایش داده می‌شود تا همهٔ featureها تأیید شوند.'],
                        changed_lines=[], cnc_text=text, dxf_text=to_dxf(vectors))
        # Safe default: approved profiles still need explicit legacy opt-in until
        # segment-level stretch rules are saved; this prevents arc distortion.
        if not profile.get('legacy_gap_resize'):
            return dict(ok=True, resizable=False, profile_mode=True, profile_errors=[
                        'ناحیهٔ خطوط صاف قابل تغییر هنوز در پروفایل ثبت نشده است'],
                        features=inventory, original=shown, resized=shown,
                        length_before=round(b-a,3), length_after=round(b-a,3), delta=0.0,
                        warnings=['برای فعال‌سازی تولید، نقاط شروع و پایان ناحیهٔ کشش را ثبت کنید.'],
                        changed_lines=[], cnc_text=text, dxf_text=to_dxf(vectors))
        legacy = dict(params)
        legacy.pop('profile', None); legacy.pop('feature_profile', None)
        result = process(text, legacy)
        result.update(profile_mode=True, features=inventory, profile_errors=[])
        result['warnings'].append('پروفایل اجرا شد؛ Z/Feed/Spindle و شعاع منحنی‌ها تغییر داده نشدند.')
        return result

    mode = params.get('mode', 'equal')
    if mode not in ('equal', 'proportional', 'manual'):
        raise CncError("روش توزیع نامعتبر است")
    delta = _num(params, 'delta', 0.0, -5000, 5000)
    tool_dia = _num(params, 'tool_dia', 8.0, 0.1, 100)
    symmetric = bool(params.get('symmetric', True))

    try:
        levels, edge_lo, edge_hi = detect_geometry(lines)
    except CncError as exc:
        if params.get('allow_view_only'):
            return view_only_result(text, lines, eol, tool_dia, str(exc))
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

    v0 = extract_vectors(lines, tool_dia)
    v1 = extract_vectors(out_lines, tool_dia)
    a0, b0 = _span(v0)
    a1, b1 = _span(v1)

    changed = [i for i, (p, q) in enumerate(zip(lines, out_lines)) if p != q]
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
        original=vectors_for_json(v0),
        resized=vectors_for_json(v1),
        warnings=warnings,
        changed_lines=changed,
        cnc_text=eol.join(out_lines) + eol,
        dxf_text=to_dxf(v1),
    )


def handle_request(raw):
    """Framework-independent API handler. raw = request body (bytes, JSON:
       {filename, content, params}).  Returns (http_status, dict)."""
    try:
        payload = json.loads(raw.decode('utf-8'))
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

# --- Generic operator-approved feature profiles ----------------------------
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
                    'start': [round(v,3) for v in block['start']]})
    return out


def validate_profile(profile, inventory):
    if not isinstance(profile, dict):
        return ['پروفایل هندسی وجود ندارد']
    errors = []
    if profile.get('axis', 'length') != 'length':
        errors.append('محور تغییر CNC باید length باشد؛ طول روی Y قرار دارد')
    rules = profile.get('features') or {}
    known = {f['id'] for f in inventory}
    for fid in rules:
        if fid not in known:
            errors.append(f'feature ناشناخته در پروفایل: {fid}')
    for item in inventory:
        rule = rules.get(item['id'])
        mode = rule.get('mode') if isinstance(rule, dict) else None
        if not rule or mode in (None, 'view_only', 'unknown'):
            errors.append(f"feature {item['id']} هنوز توسط اپراتور تأیید نشده است")
        if item['kind'] in ('outer_contour', 'hole') and mode == 'stretch':
            errors.append(f"تغییر مستقیم {item['kind']} مجاز نیست؛ فقط خطوط صاف میانی را انتخاب کنید")
    if not profile.get('reference_points'):
        errors.append('حداقل یک نقطهٔ مرجع اپراتور ثبت نشده است')
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
    inventory = analyze_program(lines)
    profile = params.get('profile') or params.get('feature_profile')
    errors = validate_profile(profile, inventory) if profile else []
    return {'ok': True, 'features': inventory, 'profile_errors': errors,
            'ready': bool(profile and not errors), 'warnings': []}
