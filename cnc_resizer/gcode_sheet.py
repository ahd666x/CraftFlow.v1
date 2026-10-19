"""Legacy generic sheet-program prototype and geometry helpers.

``build_sheet_program`` assumes G90 absolute X/Y and incremental I/J arc centres.
Those assumptions have not been tied to an approved destination controller, so
the production planner must not use this builder as a machine postprocessor.
"""
import math
import re

NUM = r"[+-]?\d+(?:\.\d+)?"
WORD_RE = re.compile(r"([A-Z])\s*(" + NUM + r")")
LINE_NO_RE = re.compile(r"^\s*N\d+\s*")
COMMENT_RE = re.compile(r"\([^)]*\)|;.*$")
TWO_PI = 2 * math.pi


class GcodeError(ValueError):
    pass


def _clean(line):
    line = COMMENT_RE.sub("", line).strip().upper()
    return LINE_NO_RE.sub("", line)


def _words(line):
    return [(m.group(1), float(m.group(2)), m.group(2)) for m in WORD_RE.finditer(line)]


def _has_unparsed(line):
    return bool(WORD_RE.sub("", line).strip())


def fmt(value):
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def split_program(text):
    """Return (preamble, body, postamble) as lists of cleaned lines.

    * preamble  - everything before the first X/Y move (tool change, spindle...)
    * postamble - trailing machine-coordinate (G53) lines such as ``G53X0Y3950``
    * ``%``, ``Oxxxx`` and ``M2/M30`` are removed.
    """
    lines = []
    for raw in text.splitlines():
        line = _clean(raw)
        if not line or line == "%" or re.fullmatch(r"O\d+", line) or line in ("M2", "M02", "M30"):
            continue
        lines.append(line)
    first = next((i for i, l in enumerate(lines) if "G53" not in l and re.search(r"[XY]" + NUM, l)), None)
    if first is None:
        raise GcodeError("فایل CNC حرکت X/Y قابل استفاده ندارد.")
    last = len(lines)
    while last > first and "G53" in lines[last - 1]:
        last -= 1
    return lines[:first], lines[first:last], lines[last:]


def _arc_extremes(sx, sy, ex, ey, i, j, clockwise):
    cx, cy = sx + i, sy + j
    radius = math.hypot(sx - cx, sy - cy)
    if radius == 0:
        return []
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    sweep = ((a0 - a1) if clockwise else (a1 - a0)) % TWO_PI
    if sweep < 1e-9:  # start == end -> full circle
        sweep = TWO_PI
    points = []
    for k in range(4):
        angle = k * math.pi / 2
        delta = ((a0 - angle) if clockwise else (angle - a0)) % TWO_PI
        if delta <= sweep:
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _arc_center_from_r(sx, sy, ex, ey, r, clockwise):
    """Center of an R-format arc; ``None`` when the geometry is impossible.

    G-code sign convention: positive R selects the minor (<= 180 deg) arc,
    negative R the major one. For a CCW arc (G3) the centre lies on the
    left side of the start->end chord; for CW (G2) on the right side.
    """
    radius = abs(r)
    dx, dy = ex - sx, ey - sy
    chord = math.hypot(dx, dy)
    if radius == 0 or chord == 0 or chord > 2 * radius:
        return None
    mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
    h = math.sqrt(radius * radius - (chord / 2.0) ** 2)
    lx, ly = -dy / chord, dx / chord  # unit normal to the left of the chord
    left = (mx + lx * h, my + ly * h)
    right = (mx - lx * h, my - ly * h)
    minor = r >= 0
    if clockwise:
        return right if minor else left
    return left if minor else right


def source_bounds(body):
    """Bounding box of the *cutting* geometry (G1/G2/G3), arcs included.

    Rapid moves (G0) - e.g. the ``G0X0Y0`` park move at the end of a part
    program - are ignored so they cannot inflate the part size.
    """
    cur_x = cur_y = None
    mode = None
    pts = []
    for line in body:
        if "G53" in line:
            continue
        words = _words(line)
        for letter, value, _ in words:
            if letter == "G" and value in (0, 1, 2, 3):
                mode = int(value)
        vals = {}
        for letter, value, _ in words:
            if letter in "XYIJR":
                vals[letter] = value
        nx = vals.get("X", cur_x)
        ny = vals.get("Y", cur_y)
        if mode in (1, 2, 3) and ("X" in vals or "Y" in vals or ("I" in vals or "J" in vals)):
            if cur_x is not None and cur_y is not None:
                pts.append((cur_x, cur_y))
            if nx is not None and ny is not None:
                pts.append((nx, ny))
            if mode in (2, 3) and cur_x is not None and cur_y is not None and nx is not None and ny is not None:
                if "I" in vals or "J" in vals:
                    pts.extend(_arc_extremes(cur_x, cur_y, nx, ny, vals.get("I", 0.0), vals.get("J", 0.0), mode == 2))
                elif "R" in vals:
                    # R-format arc: derive the centre from R and the
                    # start->end chord. Previously only I/J were read, so
                    # R arcs silently shrank the bounding box and the part
                    # could be nested overlapping its neighbours.
                    center = _arc_center_from_r(cur_x, cur_y, nx, ny, vals["R"], mode == 2)
                    if center is not None:
                        pts.extend(_arc_extremes(
                            cur_x, cur_y, nx, ny,
                            center[0] - cur_x, center[1] - cur_y, mode == 2,
                        ))
        cur_x, cur_y = nx, ny
    if not pts:
        raise GcodeError("فایل CNC مسیر برش (G1/G2/G3) با مختصات X/Y ندارد.")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def transform_body(body, bounds, place_x, place_y, rotated=False):
    """Move one part program so its bounding box starts at (place_x, place_y).

    ``rotated`` turns the part 90 degrees clockwise: (x, y) -> (y - min_y, max_x - x),
    arc centres (i, j) -> (j, -i).  A pure rotation keeps G2/G3 unchanged.
    The placed footprint is then (source height) x (source width).
    """
    min_x, min_y, max_x, _max_y = bounds
    if rotated:
        def mapper(x, y):
            return (y - min_y) + place_x, (max_x - x) + place_y
    else:
        def mapper(x, y):
            return (x - min_x) + place_x, (y - min_y) + place_y

    out = []
    cur_x = cur_y = None
    for line in body:
        if "G53" in line:
            out.append(line)
            continue
        if _has_unparsed(line):
            if re.search(r"[XYIJ]\s*" + NUM, line):
                raise GcodeError(f"خط G-code قابل تبدیل نیست: {line}")
            out.append(line)
            continue
        words = _words(line)
        vals = {l: v for l, v, _ in words if l in "XYIJ"}
        if "X" in vals:
            cur_x = vals["X"]
        if "Y" in vals:
            cur_y = vals["Y"]
        has_xy = "X" in vals or "Y" in vals
        if not rotated:
            parts = []
            for letter, value, raw in words:
                if letter == "X":
                    parts.append("X" + fmt(mapper(value, 0)[0]))
                elif letter == "Y":
                    parts.append("Y" + fmt(mapper(0, value)[1]))
                else:
                    parts.append(f"{letter}{raw}")
            out.append(" ".join(parts))
            continue
        new_x = new_y = None
        if has_xy:
            if cur_x is None or cur_y is None:
                raise GcodeError(f"برای چرخش قطعه، مختصات قبلی خط مشخص نیست: {line}")
            new_x, new_y = mapper(cur_x, cur_y)
        parts = []
        xy_done = False
        for letter, _value, raw in words:
            if letter in "XY":
                if not xy_done:  # a rotated move always needs both axes
                    parts.extend(["X" + fmt(new_x), "Y" + fmt(new_y)])
                    xy_done = True
            elif letter in "IJ":
                continue
            else:
                parts.append(f"{letter}{raw}")
        if "I" in vals or "J" in vals:  # arc centre: (i, j) -> (j, -i)
            i, j = vals.get("I", 0.0), vals.get("J", 0.0)
            parts.extend(["I" + fmt(j), "J" + fmt(-i)])
        out.append(" ".join(parts))
    return out


def build_sheet_program(label, placements):
    """Assemble one program from placed parts.

    ``placements``: list of dicts with ``text``, ``place_x``, ``place_y``,
    ``rotated`` and ``name``.  The tool preamble and the final machine park
    (G53) lines are emitted once; a part's preamble is repeated only if it
    differs from the previous one (e.g. another tool).
    """
    if not placements:
        raise GcodeError("برای ساخت برنامه ورق حداقل یک قطعه لازم است.")
    first_pre = first_post = None
    previous_pre = None
    chunks = []
    for item in placements:
        pre, body, post = split_program(item["text"])
        bounds = source_bounds(body)
        moved = transform_body(body, bounds, item["place_x"], item["place_y"], item.get("rotated", False))
        if first_pre is None:
            first_pre, first_post = pre, post
        chunks.append(f"; PART {item['name']}")
        if pre != previous_pre and previous_pre is not None:
            chunks.extend(pre)
        chunks.extend(moved)
        previous_pre = pre
    lines = ["%", f"; {label}", *first_pre, *chunks, *first_post, "M30", "%"]
    return "\n".join(lines) + "\n"
