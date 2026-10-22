"""Part-level interactive edits lowered onto the proven block machinery.

Every edit the operator makes on a *part* (the outer contour plus all of its
children and assigned operations, at every depth pass) is lowered here into
per-block transforms that already exist and are runtime-proven:

* ``move_part``            -> one :func:`_shift_block_rigid` per member block
                              (identical delta for every depth pass, so the
                              depth-repeat group rule holds by construction);
* ``stretch_part``         -> an axis-generalised :func:`_stretch_feature_x`
                              per contour pass (straight axial moves inside
                              the band absorb the change, arcs and diagonals
                              are rejected; children stay anchored to the
                              lower edge by default, and wholly upper parts
                              follow the expanded contour);
* ``stretch_chain``        -> equal per-part axis stretch; members may sit
                              STACKED (lower edges follow the finished growth
                              of the column below them: offsets 0, d, 2d, ...)
                              or SIDE BY SIDE (doors cut from one G-code —
                              each grows at its own band with offset 0);
* ``set_gap`` / ``distribute`` -> rigid shifts derived from the FINISHED
                              bounding boxes (tool diameter compensated).

The candidate must serialize as a pure coordinate patch
(``serializer.serialize_candidate``), is proven at runtime by reparsing the
patched draft (block/move counts, Z untouched, per-block bounding boxes,
untouched blocks byte-stable) and passes the part-level collision gate on the
runtime geometry: a NEW partial overlap between two finished part boxes is
rejected, containment stays legitimate nesting, and a pre-existing partial
overlap (interlocking contours) may only persist when the relative
arrangement of the pair is preserved. Nothing here weakens the fail-closed
chain — machine output still flows through ``controller.postprocess_resize``.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from . import parts as parts_mod
from .geometry import block_geometry
from .ir import Program, to_dict
from .parametric import TransformError, _shift_block_rigid
from .parser import parse_program
from .serializer import SerializerBlocked, serialize_candidate

PART_OP_LIST = ("move_part", "stretch_part", "stretch_chain", "set_gap", "distribute")
_RUNTIME_TOL = 1.5e-3     # serializer writes 3 decimals: half a micron of slack
_FINITE_TOL = 1e-6
# The canvas sends stretch-band endpoints rounded to 0.001 units. Snap only
# near-identical endpoints back to the detected straight interval; never use
# this tolerance to admit a band that crosses an arc or a slanted edge.
_BAND_ENDPOINT_TOL = 5.1e-4
# a relocated stretch band must keep this clearance from every child path
# (far above the 3-decimal serializer rounding, far below operator scale)
_BAND_CLEAR_MARGIN = 0.25
# a clear sub-band shorter than this cannot host the cut meaningfully
_BAND_MIN_CLEAR = 0.5


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TransformError(f"{label} باید عدد باشد") from exc
    if not math.isfinite(number):
        raise TransformError(f"{label} باید متناهی باشد")
    return number


def _stretch_feature_axis(block: Any, axis: str, feature_id: str,
                          band_start: float, band_end: float,
                          below_offset: float, above_offset: float) -> None:
    """Segment-level stretch of one block along ``axis`` (X or Y).

    Axis-generalised twin of ``parametric._stretch_feature_x`` with the same
    fail-closed guards: only straight moves parallel to the stretch axis may
    bridge the band, arcs never take different offsets at their two ends, and
    moved modal endpoints are materialized by the serializer as explicit
    absolute axis words on their original source lines.
    """
    tol = _FINITE_TOL
    cross = "x" if axis == "y" else "y"
    motions = block.motions
    moves = [move for move in motions if move.motion in ("linear", "arc_cw", "arc_ccw")]
    if not moves:
        raise TransformError(f"Feature {feature_id}: بلوک حرکت برشی برای کشش ندارد")
    first = moves[0]
    if (first.start is None or getattr(first.start, axis) is None
            or getattr(first.start, cross) is None
            or first.end is None or getattr(first.end, axis) is None
            or getattr(first.end, cross) is None):
        raise TransformError(f"Feature {feature_id}: نخستین حرکت برشی مختصات حل‌نشده دارد")
    entry = motions[0]
    if entry.motion != "rapid" or entry.machine_coordinates:
        raise TransformError(
            f"Feature {feature_id}: بلوک با rapid موقعیت‌یابی مستقل شروع نمی‌شود؛ "
            "کشش باعث برش مورب از نقطهٔ ورود کهنه می‌شود")
    if entry.end is None or getattr(entry.end, axis) is None or getattr(entry.end, cross) is None:
        raise TransformError(f"Feature {feature_id}: rapid موقعیت‌یابی مختصات حل‌نشده دارد")
    if (abs(getattr(entry.end, axis) - getattr(first.start, axis)) > tol
            or abs(getattr(entry.end, cross) - getattr(first.start, cross)) > tol):
        raise TransformError(
            f"Feature {feature_id}: rapid موقعیت‌یابی به نخستین حرکت برشی ختم نمی‌شود؛ ساختار بلوک پشتیبانی نمی‌شود")
    for move in motions[1:]:
        if move.motion == "rapid" and ("x" in (move.raw_words or {}) or "y" in (move.raw_words or {})):
            raise TransformError(
                f"Feature {feature_id}: rapid جابه‌جایی داخلی پشتیبانی نمی‌شود؛ بلوک فقط باید rapid فقط-Z داشته باشد")

    junctions = [getattr(moves[0].start, axis)] + [getattr(move.end, axis) for move in moves]
    if any(value is None for value in junctions):
        raise TransformError(f"Feature {feature_id}: مختصات {axis.upper()} حل‌نشده در زنجیرهٔ حرکت")
    if min(junctions) > band_start + tol or max(junctions) < band_end - tol:
        raise TransformError(
            f"Feature {feature_id}: نقاط extremes مسیر روی برآمدگی قوس است نه endpoint حرکت؛ "
            "کشش پاره‌ای به endpoint در هر دو край نیاز دارد")

    def junction_offset(value: float) -> float:
        if value <= band_start + tol:
            return below_offset
        if value >= band_end - tol:
            return above_offset
        return 0.0

    offsets = [junction_offset(value) for value in junctions]

    previous_end = None
    for index, move in enumerate(moves):
        if (move.start is None or move.end is None
                or getattr(move.start, axis) is None or getattr(move.end, axis) is None
                or getattr(move.start, cross) is None or getattr(move.end, cross) is None):
            raise TransformError(f"Feature {feature_id}: حرکت {move.id} مختصات حل‌نشده دارد")
        if previous_end is not None and (
                abs(getattr(move.start, axis) - previous_end[0]) > tol
                or abs(getattr(move.start, cross) - previous_end[1]) > tol):
            raise TransformError(
                f"Feature {feature_id}: زنجیرهٔ حرکت پیش از {move.id} گسسته است")
        previous_end = (getattr(move.end, axis), getattr(move.end, cross))
        if abs(offsets[index] - offsets[index + 1]) > 1e-9:
            if move.motion != "linear":
                raise TransformError(
                    f"Feature {feature_id}: کشش قوس‌ها را صلب نگه می‌دارد؛ قوس با دو آفست متفاوت در دو سر خراب می‌شود")
            if abs(getattr(move.end, cross) - getattr(move.start, cross)) > tol:
                raise TransformError(
                    f"Feature {feature_id}: کشش فقط خطوط صاف مواز {axis.upper()} را بلند می‌کند؛ "
                    f"حرکت با تغییر {cross.upper()} کج می‌شود")
            if axis not in (move.raw_words or {}):
                raise TransformError(
                    f"Feature {feature_id}: حرکت {move.id} در محور {axis.upper()} از مختصات modal "
                    "استفاده می‌کند و نمی‌تواند مرز کشش را به‌طور امن قطع کند")

    if abs(offsets[0]) > 1e-9:
        setattr(entry.end, axis, getattr(entry.end, axis) + offsets[0])
        # NOTE: the parser aliases block.start to the entry rapid's end Point3
        # (start=end), so block.start already moved with entry.end.
    for index, move in enumerate(moves):
        start_offset, end_offset = offsets[index], offsets[index + 1]
        # Motion start/end are independent Point3 instances in the IR, not
        # aliases of the previous endpoint. Update both to keep the rendered
        # path connected and preserve the vector of every rigidly translated
        # diagonal. Only the endpoint is serialized on its source line; the
        # start is the resolved modal position used by preview/geometry.
        if abs(start_offset) > 1e-12:
            setattr(move.start, axis, getattr(move.start, axis) + start_offset)
        if abs(end_offset) > 1e-12:
            setattr(move.end, axis, getattr(move.end, axis) + end_offset)
        if move.arc is not None:
            if abs(start_offset) > 1e-12 and move.arc.center and axis in move.arc.center:
                move.arc.center[axis] += start_offset


def _band_mapping(band_start: float, band_end: float, below: float, above: float):
    def junction_offset(value: float) -> float:
        if value <= band_start + _FINITE_TOL:
            return below
        if value >= band_end - _FINITE_TOL:
            return above
        return 0.0
    return junction_offset


def _member_blocks(part: dict[str, Any]) -> list[str]:
    """Every block that physically belongs to the part: contour passes at all
    depths, closed children (holes/pockets) and assigned open operations."""
    block_ids = list(part["contour_block_ids"])
    for child in part["children"]:
        block_ids.extend(child["block_ids"])
    return sorted(set(block_ids))


def _combined_bbox(geometries: list[dict[str, Any]]) -> dict[str, float] | None:
    boxes = [item["bounds"] for item in geometries if item["bounds"]]
    if not boxes:
        return None
    return {
        "x_min": min(box["x_min"] for box in boxes),
        "y_min": min(box["y_min"] for box in boxes),
        "x_max": max(box["x_max"] for box in boxes),
        "y_max": max(box["y_max"] for box in boxes),
    }


def _interval_state(bbox: dict[str, float], band: tuple[float, float], axis: str) -> str:
    """below / above / straddle of one bbox relative to the band on ``axis``."""
    low, high = bbox[f"{axis}_min"], bbox[f"{axis}_max"]
    if high <= band[0] + _FINITE_TOL:
        return "below"
    if low >= band[1] - _FINITE_TOL:
        return "above"
    return "straddle"


def _ranges_share_area(a: dict[str, float], b: dict[str, float],
                       lo_key: str, hi_key: str) -> bool:
    """True when the two 1-D ranges share actual length (edge touching does
    not count as sharing — a zero-width contact cannot tear geometry)."""
    return a[lo_key] < b[hi_key] - _FINITE_TOL and a[hi_key] > b[lo_key] + _FINITE_TOL


def _boxes_overlap_2d(a: dict[str, float], b: dict[str, float]) -> bool:
    """True when two bounding boxes share AREA on the sheet. Parts that are
    merely side by side (disjoint on one axis) are NOT overlapping — a
    stretch confined to one part's contour cannot tear its neighbour."""
    return (_ranges_share_area(a, b, "x_min", "x_max")
            and _ranges_share_area(a, b, "y_min", "y_max"))


def _clear_sub_bands(interval: tuple[float, float],
                     blocked: list[tuple[float, float]],
                     margin: float) -> list[tuple[float, float]]:
    """Sub-intervals of ``interval`` that keep ``margin`` clearance from every
    blocked (lo, hi) span — the zones a stretch cut can live in without
    tearing a child path."""
    spans = sorted((lo - margin, hi + margin) for lo, hi in blocked)
    out: list[tuple[float, float]] = []
    cursor = interval[0]
    for lo, hi in spans:
        if hi <= cursor:
            continue
        if lo > cursor:
            out.append((cursor, min(lo, interval[1])))
        cursor = max(cursor, hi)
        if cursor >= interval[1]:
            break
    if interval[1] - cursor > 0:
        out.append((cursor, interval[1]))
    return [(lo, hi) for lo, hi in out if hi - lo >= _BAND_MIN_CLEAR]


def _relocate_band(intervals: list[tuple[float, float]],
                   requested: tuple[float, float],
                   child_boxes: list[dict[str, float]],
                   axis: str) -> tuple[float, float] | None:
    """Nearest clear band for the stretch cut — the same semantic the legacy
    gap-resize engine had: the extra length is inserted in a gap BETWEEN the
    child features (hole rows), never through them.  Nearest centre to the
    requested band wins; overlap only breaks ties."""
    blocked = [(box[f"{axis}_min"], box[f"{axis}_max"]) for box in child_boxes]
    requested_center = (requested[0] + requested[1]) / 2
    best: tuple[float, float] | None = None
    best_key: tuple[float, float] | None = None
    for interval in intervals:
        for lo, hi in _clear_sub_bands(interval, blocked, _BAND_CLEAR_MARGIN):
            overlap = min(requested[1], hi) - max(requested[0], lo)
            center = (lo + hi) / 2
            key = (abs(center - requested_center), -overlap)
            if best_key is None or key < best_key:
                best, best_key = (lo, hi), key
    return best


def preview_part_edits(program: Program, request: dict[str, Any], source_text: str,
                       params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Preview part-level edits; never returns G-code text (same contract as
    ``parametric.preview_edits``)."""
    if not isinstance(request, dict):
        raise TransformError("بدنهٔ درخواست ویرایش‌ها باید یک شیء باشد")
    edits = request.get("edits")
    if not isinstance(edits, list) or not edits:
        raise TransformError("edits باید فهرست غیرخالی باشد")

    params = params or {}
    detection = parts_mod.part_index(program, params)
    part_by_id = {part["id"]: part for part in detection["parts"]}
    orphan_ops = [op for op in detection["operations"] if op["assigned_part"] is None]
    source_geometry = {block.id: block_geometry(block) for block in program.blocks}

    # lowered transforms: block_id -> action
    lowering: dict[str, dict[str, Any]] = {}
    touched_parts: set[str] = set()

    def assign_shift(block_ids: list[str], dx: float, dy: float, label: str) -> None:
        for block_id in block_ids:
            existing = lowering.get(block_id)
            if existing is not None:
                if (existing["op"] != "shift" or abs(existing["dx"] - dx) > 1e-9
                        or abs(existing["dy"] - dy) > 1e-9):
                    raise TransformError(
                        f"{label}: بلوک {block_id} قبلاً توسط ویرایش دیگری تغییر کرده؛ "
                        "هر بلوک در یک درخواست فقط یک تبدیل می‌گیرد")
                continue
            lowering[block_id] = {"op": "shift", "dx": dx, "dy": dy}

    def assign_stretch(block_ids: list[str], axis: str, band: tuple[float, float],
                       delta: float, label: str, below_offset: float = 0.0) -> None:
        for block_id in block_ids:
            existing = lowering.get(block_id)
            if existing is not None:
                raise TransformError(
                    f"{label}: بلوک {block_id} قبلاً توسط ویرایش دیگری تغییر کرده؛ "
                    "هر بلوک در یک درخواست فقط یک تبدیل می‌گیرد")
            lowering[block_id] = {"op": "stretch", "axis": axis,
                                  "band_start": band[0], "band_end": band[1],
                                  "below_offset": below_offset,
                                  "above_offset": below_offset + delta}

    def part_or_error(part_id: Any, label: str) -> dict[str, Any]:
        if not isinstance(part_id, str) or part_id not in part_by_id:
            raise TransformError(f"{label}: part_id ناشناخته است: {part_id!r}")
        return part_by_id[part_id]

    def require_free(part: dict[str, Any], label: str) -> None:
        if part["locked"]:
            raise TransformError(
                f"{label}: قطعهٔ {part['id']} قفل است (کانتورهای هم‌پوشان)؛ "
                "ویرایش آن غیرفعال است — از میزکار ویژگی‌محور استفاده کنید")
        if part["id"] in touched_parts:
            raise TransformError(f"{label}: قطعهٔ {part['id']} در این درخواست دوبار ویرایش شده است")

    normalized_summary: list[dict[str, Any]] = []
    band_relocation_notes: list[str] = []
    # bugfix: pure-translation bookkeeping so a later set_gap/distribute in the
    # SAME request composes against the moved position instead of the source
    # one ("move part_a, then set_gap to part_b" must measure from where
    # part_a actually is now). Stretches are not pure translations, so any
    # set_gap that references a stretched part is rejected instead.
    part_shift: dict[str, tuple[float, float]] = {}
    part_stretched: set[str] = set()

    def record_shift(part_id: str, dx: float, dy: float) -> None:
        previous = part_shift.get(part_id, (0.0, 0.0))
        part_shift[part_id] = (previous[0] + dx, previous[1] + dy)

    def shifted_bbox(bbox: dict[str, float], shift: tuple[float, float]) -> dict[str, float]:
        dx, dy = shift
        return {"x_min": bbox["x_min"] + dx, "x_max": bbox["x_max"] + dx,
                "y_min": bbox["y_min"] + dy, "y_max": bbox["y_max"] + dy}

    for index, edit in enumerate(edits):
        label = f"Edit {index + 1}"
        if not isinstance(edit, dict) or edit.get("op") not in PART_OP_LIST:
            raise TransformError(
                f"{label}: op باید یکی از " + "، ".join(PART_OP_LIST) + " باشد")
        op = edit["op"]
        if op == "move_part":
            part = part_or_error(edit.get("part_id"), label)
            require_free(part, label)
            dx = _finite(edit.get("dx"), f"{label} dx")
            dy = _finite(edit.get("dy"), f"{label} dy")
            if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
                raise TransformError(f"{label}: جابه‌جایی صفر است")
            assign_shift(_member_blocks(part), dx, dy, label)
            record_shift(part["id"], dx, dy)
            touched_parts.add(part["id"])
            normalized_summary.append({"op": op, "part_id": part["id"], "dx": dx, "dy": dy,
                                       "blocks": _member_blocks(part)})
        elif op == "stretch_part":
            part = part_or_error(edit.get("part_id"), label)
            require_free(part, label)
            axis = edit.get("axis")
            if axis not in ("x", "y"):
                raise TransformError(f"{label}: axis باید x یا y باشد")
            has_delta = edit.get("delta") is not None
            has_new_size = edit.get("new_size") is not None
            if has_delta and has_new_size:
                raise TransformError(f"{label}: فقط یکی از delta یا new_size را بفرستید")
            if has_new_size:
                new_size = _finite(edit.get("new_size"), f"{label} new_size")
                current = part["bbox"][f"{axis}_max"] - part["bbox"][f"{axis}_min"]
                if new_size <= 0:
                    raise TransformError(f"{label}: new_size باید بزرگ‌تر از صفر باشد")
                delta = new_size - current
            elif has_delta:
                delta = _finite(edit.get("delta"), f"{label} delta")
            else:
                raise TransformError(f"{label}: کشش به delta یا new_size نیاز دارد")
            if abs(delta) <= 1e-9:
                raise TransformError(f"{label}: دلتای کشش صفر است")
            capability = part["capabilities"].get(f"can_stretch_{axis}")
            band_given = edit.get("band")
            if band_given is not None:
                if (not isinstance(band_given, list) or len(band_given) != 2):
                    raise TransformError(f"{label}: band باید [start, end] باشد")
                band = (_finite(band_given[0], f"{label} band[0]"),
                        _finite(band_given[1], f"{label} band[1]"))
                if band[1] - band[0] <= _FINITE_TOL:
                    raise TransformError(f"{label}: طول نوار باید مثبت باشد")
                canonical = part["_canonical"]
                intervals = parts_mod._band_intervals(canonical, axis, params.get("merge_tol", 0.6))
                snapped_band = next((
                    (lo if abs(band[0] - lo) <= _BAND_ENDPOINT_TOL else band[0],
                     hi if abs(band[1] - hi) <= _BAND_ENDPOINT_TOL else band[1])
                    for lo, hi in intervals
                    if band[0] >= lo - _BAND_ENDPOINT_TOL
                    and band[1] <= hi + _BAND_ENDPOINT_TOL
                ), None)
                if snapped_band is None:
                    raise TransformError(
                        f"{label}: نوار روی قوس یا لبهٔ مایل می‌گذرد؛ نوار را فقط روی "
                        "خطوط صاف موازی محور انتخاب کنید (نگاه کنید به stretch_band_"
                        f"{axis} همین قطعه)")
                band = snapped_band
            else:
                band_value = part.get(f"stretch_band_{axis}")
                if not capability or not band_value:
                    raise TransformError(
                        f"{label}: قطعهٔ {part['id']} نوار صاف {axis.upper()} برای کشش ندارد "
                        "(قوس/لبهٔ مایل خراب می‌شود)؛ فقط جابه‌جایی/چرخش/آینه در دسترس است")
                band = (band_value[0], band_value[1])
            contour_ids = list(part["contour_block_ids"])
            # Parametric default: child features are anchored to the lower
            # edge (min Y / min X).  A child is therefore not pushed with the
            # upper side of the cut band; its distance from the reference edge
            # remains unchanged.  Since only the parent contour is stretched,
            # a child may safely lie across the band.
            band_relocated = None
            assign_stretch(contour_ids, axis, band, delta, label)
            part_stretched.add(part["id"])
            # everything else on the sheet follows the band: rigid beyond it
            member_ids = set(_member_blocks(part))
            for other in detection["parts"]:
                if other["id"] == part["id"]:
                    continue
                state = _interval_state(other["bbox"], band, axis)
                if state == "below":
                    continue
                if state == "above":
                    if other["id"] in touched_parts:
                        raise TransformError(
                            f"{label}: قطعهٔ {other['id']} دوبار ویرایش شده است (هم پیرو کشش، هم صریح)")
                    assign_shift(_member_blocks(other), delta if axis == "x" else 0.0,
                                 delta if axis == "y" else 0.0, label)
                    record_shift(other["id"], delta if axis == "x" else 0.0,
                                 delta if axis == "y" else 0.0)
                    touched_parts.add(other["id"])
                    continue
                # straddle: harmless when the neighbour is laterally separated
                # (2D-disjoint boxes, e.g. doors side by side on one sheet) —
                # the band cut lives inside THIS part's contour only, so a
                # side-by-side part is never torn and simply stays put.
                if not _boxes_overlap_2d(other["bbox"], part["bbox"]):
                    continue
                raise TransformError(
                    f"{label}: قطعهٔ {other['id']} روی نوار کشش قرار دارد و کشیدن آن را پاره می‌کند؛ "
                    "نوار را فقط داخل قطعهٔ انتخابی بگذارید")
            for orphan in orphan_ops:
                state = _interval_state(orphan["bbox"], band, axis)
                if state == "below":
                    continue
                if state == "above":
                    assign_shift(orphan["block_ids"], delta if axis == "x" else 0.0,
                                 delta if axis == "y" else 0.0, label)
                    continue
                if not _boxes_overlap_2d(orphan["bbox"], part["bbox"]):
                    continue
                raise TransformError(
                    f"{label}: عملیات {orphan['id']} روی نوار کشش می‌گذرد و کشیدن آن را خراب می‌کند")
            touched_parts.add(part["id"])
            summary_entry = {"op": op, "part_id": part["id"], "axis": axis,
                             "band": [band[0], band[1]], "delta": delta,
                             "blocks": contour_ids}
            if band_relocated:
                summary_entry["band_relocated"] = band_relocated
                band_relocation_notes.append(
                    f"{label}: نوار کشش قطعهٔ {part['id']} روی فرزند می‌افتاد؛ برش کشش به "
                    f"نزدیک‌ترین فاصلهٔ خالی بین فرزندها منتقل شد "
                    f"({band_relocated['from'][0]:.1f}–{band_relocated['from'][1]:.1f} ← "
                    f"{band_relocated['to'][0]:.1f}–{band_relocated['to'][1]:.1f} mm روی "
                    f"{axis.upper()}).")
            normalized_summary.append(summary_entry)
        elif op == "stretch_chain":
            axis = edit.get("axis")
            if axis not in ("x", "y"):
                raise TransformError(f"{label}: axis باید x یا y باشد")
            raw_ids = edit.get("parts")
            if (not isinstance(raw_ids, list) or len(raw_ids) < 2
                    or any(not isinstance(item, str) for item in raw_ids)):
                raise TransformError(f"{label}: parts باید فهرست حداقل دو شناسهٔ قطعه باشد")
            if len(set(raw_ids)) != len(raw_ids):
                raise TransformError(f"{label}: فهرست parts تکراری دارد")
            delta = _finite(edit.get("delta"), f"{label} delta")
            if abs(delta) <= 1e-9:
                raise TransformError(f"{label}: دلتای هر قطعه نباید صفر باشد")
            chain = [part_or_error(item, label) for item in raw_ids]
            for part in chain:
                require_free(part, label)
                if not part["capabilities"].get(f"can_stretch_{axis}"):
                    raise TransformError(
                        f"{label}: قطعهٔ {part['id']} نوار صاف {axis.upper()} برای کشش ندارد")
            lo_key, hi_key = f"{axis}_min", f"{axis}_max"
            cross_lo, cross_hi = ("y_min", "y_max") if axis == "x" else ("x_min", "x_max")
            chain.sort(key=lambda item: item["bbox"][lo_key])
            # Lateral awareness: a sheet often carries several copies of the
            # same part SIDE BY SIDE (three doors cut from one G-code). Such
            # members are 2D-disjoint and stretch independently; only members
            # whose boxes share actual AREA are rejected as interlocked. A
            # pair disjoint along the axis but sharing the cross axis keeps
            # the stacked follow semantics below.
            for index_a in range(len(chain)):
                for index_b in range(index_a + 1, len(chain)):
                    if _boxes_overlap_2d(chain[index_a]["bbox"], chain[index_b]["bbox"]):
                        raise TransformError(
                            f"{label}: قطعه‌های زنجیرهٔ {chain[index_a]['id']} و "
                            f"{chain[index_b]['id']} روی هم افتاده‌اند (هم‌پوشانی دوبعدی)؛ "
                            "کشش زنجیره‌ای برای قطعات درهم‌تنیده تعریف‌پذیر نیست")

            # A member's lower edge follows the finished growth of every
            # cross-axis-overlapping member stacked fully below it (upper
            # offset), so equal +deltas still produce 0, d, 2d, ... down a
            # column while laterally separated members stay at offset 0 and
            # each grows at its own band.
            below_offsets: dict[str, float] = {}
            upper_offsets: dict[str, float] = {}
            selected_ids = {part["id"] for part in chain}
            for part in chain:  # sorted by lower edge: leaders resolve first
                below = 0.0
                for leader in chain:
                    if leader["id"] not in upper_offsets:
                        continue
                    if (_ranges_share_area(leader["bbox"], part["bbox"], cross_lo, cross_hi)
                            and leader["bbox"][hi_key] <= part["bbox"][lo_key] + _FINITE_TOL):
                        below = max(below, upper_offsets[leader["id"]])
                below_offsets[part["id"]] = below
                upper_offsets[part["id"]] = below + delta
            for rank, part in enumerate(chain):
                band_value = part.get(f"stretch_band_{axis}")
                if not band_value:
                    raise TransformError(
                        f"{label}: قطعهٔ {part['id']} بازهٔ صاف {axis.upper()} ندارد")
                band = (band_value[0], band_value[1])
                contour_ids = list(part["contour_block_ids"])
                below = below_offsets[part["id"]]
                assign_stretch(contour_ids, axis, band, delta, label,
                               below_offset=below)
                if abs(below) > 1e-9:
                    for child in part["children"]:
                        assign_shift(child["block_ids"],
                                     below if axis == "x" else 0.0,
                                     below if axis == "y" else 0.0, label)
                    for feature in detection["operations"]:
                        if feature.get("assigned_part") == part["id"]:
                            assign_shift(feature["block_ids"],
                                         below if axis == "x" else 0.0,
                                         below if axis == "y" else 0.0, label)
                touched_parts.add(part["id"])
                part_stretched.add(part["id"])
                normalized_summary.append({
                    "op": op, "part_id": part["id"], "axis": axis,
                    "rank": rank + 1, "delta": delta,
                    "lower_offset": below,
                    "upper_offset": below + delta,
                    "child_anchor": "lower", "band": list(band),
                    "blocks": contour_ids,
                })

            # Non-selected geometry: a piece interlocking a member (shared
            # area) is rejected fail-closed; a piece laterally separated from
            # every member is untouched; a piece stacked above members in its
            # own column follows the largest finished upper offset below it
            # (once per growth layer, not once per member).
            def _follower_shift(box: dict[str, float], kind: str, piece_id: str) -> float:
                shift = 0.0
                for member in chain:
                    if _boxes_overlap_2d(box, member["bbox"]):
                        raise TransformError(
                            f"{label}: {kind} {piece_id} با قطعهٔ زنجیرهٔ "
                            f"{member['id']} هم‌پوشانی دوبعدی دارد؛ آن را هم انتخاب کنید "
                            "یا زنجیره را بازبینی کنید")
                    if (_ranges_share_area(member["bbox"], box, cross_lo, cross_hi)
                            and member["bbox"][hi_key] <= box[lo_key] + _FINITE_TOL):
                        shift = max(shift, upper_offsets[member["id"]])
                return shift

            for other in detection["parts"]:
                if other["id"] in selected_ids:
                    continue
                shift = _follower_shift(other["bbox"], "قطعهٔ انتخاب‌نشدهٔ", other["id"])
                if abs(shift) > 1e-9:
                    assign_shift(_member_blocks(other), shift if axis == "x" else 0.0,
                                 shift if axis == "y" else 0.0, label)
                    record_shift(other["id"], shift if axis == "x" else 0.0,
                                 shift if axis == "y" else 0.0)
                    touched_parts.add(other["id"])

            for orphan in orphan_ops:
                shift = _follower_shift(orphan["bbox"], "عملیاتِ بدون مالک", orphan["id"])
                if abs(shift) > 1e-9:
                    assign_shift(orphan["block_ids"], shift if axis == "x" else 0.0,
                                 shift if axis == "y" else 0.0, label)
        elif op == "set_gap":
            part_a = part_or_error(edit.get("part_a"), label)
            part_b = part_or_error(edit.get("part_b"), label)
            if part_a["id"] == part_b["id"]:
                raise TransformError(f"{label}: part_a و part_b باید متفاوت باشند")
            require_free(part_b, label)
            axis = edit.get("axis")
            if axis not in ("x", "y"):
                raise TransformError(f"{label}: axis باید x یا y باشد")
            gap = _finite(edit.get("gap"), f"{label} gap")
            if gap < 0:
                raise TransformError(f"{label}: فاصله نمی‌تواند منفی باشد؛ هم‌پوشانی رد می‌شود")
            # bugfix: part_a may have been moved EARLIER in this same request;
            # the gap must be measured from where part_a actually is now.
            if part_a["id"] in part_stretched:
                raise TransformError(
                    f"{label}: قطعهٔ {part_a['id']} در همین درخواست کشیده شده است؛ bbox آن "
                    "دیگر یک جابه‌جایی صلب نیست و set_gap روی موقعیت جدید آن قابل ترکیب نیست — "
                    "فاصله را در یک درخواست جداگانه تنظیم کنید")
            a_shift = part_shift.get(part_a["id"], (0.0, 0.0))
            a_fin = shifted_bbox(part_a["bbox_finished"], a_shift)
            b_fin = part_b["bbox_finished"]
            center_a = (a_fin[f"{axis}_min"] + a_fin[f"{axis}_max"]) / 2
            center_b = (b_fin[f"{axis}_min"] + b_fin[f"{axis}_max"]) / 2
            if center_b >= center_a:
                translation = a_fin[f"{axis}_max"] + gap - b_fin[f"{axis}_min"]
            else:
                translation = a_fin[f"{axis}_min"] - gap - b_fin[f"{axis}_max"]
            if abs(translation) <= 1e-9:
                raise TransformError(f"{label}: این فاصله همین حالا برقرار است؛ چیزی تغییر نمی‌کند")
            dx = translation if axis == "x" else 0.0
            dy = translation if axis == "y" else 0.0
            assign_shift(_member_blocks(part_b), dx, dy, label)
            record_shift(part_b["id"], dx, dy)
            touched_parts.add(part_b["id"])
            normalized_summary.append({"op": op, "part_a": part_a["id"], "part_b": part_b["id"],
                                       "axis": axis, "gap": gap, "translation": translation,
                                       "blocks": _member_blocks(part_b)})
        else:  # distribute
            axis = edit.get("axis")
            if axis not in ("x", "y"):
                raise TransformError(f"{label}: axis باید x یا y باشد")
            gap = _finite(edit.get("gap"), f"{label} gap")
            if gap < 0:
                raise TransformError(f"{label}: gap must not be negative")
            ids = edit.get("parts")
            if not isinstance(ids, list) or len(ids) < 2 or any(not isinstance(item, str) for item in ids):
                raise TransformError(f"{label}: parts باید فهرست حداقل دو شناسهٔ قطعه باشد")
            if len(set(ids)) != len(ids):
                raise TransformError(f"{label}: فهرست parts تکراری دارد")
            chain = [part_or_error(item, label) for item in ids]
            for part in chain:
                require_free(part, label)
            chain.sort(key=lambda item: item["bbox_finished"][f"{axis}_min"])
            accumulated: dict[str, float] = {}
            previous = chain[0]
            normalized_summary.append({"op": op, "axis": axis, "gap": gap,
                                       "order": [part["id"] for part in chain]})
            for part in chain[1:]:
                target_min = (previous["bbox_finished"][f"{axis}_max"] + gap
                              + accumulated.get(previous["id"], 0.0))
                current_min = part["bbox_finished"][f"{axis}_min"] + accumulated.get(part["id"], 0.0)
                translation = target_min - current_min
                if abs(translation) > 1e-9:
                    accumulated[part["id"]] = accumulated.get(part["id"], 0.0) + translation
                    assign_shift(_member_blocks(part),
                                 translation if axis == "x" else 0.0,
                                 translation if axis == "y" else 0.0, label)
                    record_shift(part["id"], translation if axis == "x" else 0.0,
                                 translation if axis == "y" else 0.0)
                previous = part
            for part in chain:
                touched_parts.add(part["id"])

    if not lowering:
        raise TransformError("ویرایش‌ها به هیچ تغییر هندسی ختم نمی‌شوند")

    transformed = copy.deepcopy(program)
    blocks = {block.id: block for block in transformed.blocks}
    for block_id, item in lowering.items():
        block = blocks.get(block_id)
        if block is None:
            raise TransformError(f"بلوک {block_id} از برنامه حذف شده است")
        if item["op"] == "shift":
            _shift_block_rigid(block, item["dx"], item["dy"], block_id)
        else:
            _stretch_feature_axis(block, item["axis"], block_id,
                                  item["band_start"], item["band_end"],
                                  item["below_offset"], item["above_offset"])

    provisional = {"validation": {"passed": True, "errors": [], "warnings": []},
                   "source_sha256": program.source_sha256,
                   "program_ir": to_dict(transformed)}
    try:
        draft = serialize_candidate(source_text, provisional)
    except SerializerBlocked as exc:
        blockers = getattr(exc, "blockers", None) or [str(exc)]
        raise TransformError(
            "ویرایش قطعه به‌صورت وصلهٔ مختصاتی امن قابل بیان نیست: " + "؛ ".join(blockers)) from exc

    # ---- runtime proof on the patched draft ----
    errors: list[str] = []
    runtime = parse_program(draft["text"])
    if len(runtime.moves) != len(program.moves) or len(runtime.blocks) != len(program.blocks):
        errors.append("Patched draft changed the program structure (move/block count)")
    else:
        for before, after in zip(program.moves, runtime.moves):
            z_before = before.end.z if before.end is not None else None
            z_after = after.end.z if after.end is not None else None
            if z_before != z_after:
                errors.append(f"Move {before.id} changed Z at runtime")
                break
        if not errors:
            runtime_geometry = {block.id: block_geometry(block) for block in runtime.blocks}

            def bbox_equal(actual: dict[str, float] | None, expected: dict[str, float] | None,
                           message: str) -> None:
                if actual is None or expected is None:
                    if (actual is None) != (expected is None):
                        errors.append(message + " (bounds unavailable)")
                    return
                for key in ("x_min", "y_min", "x_max", "y_max"):
                    if abs(actual[key] - expected[key]) > _RUNTIME_TOL:
                        errors.append(
                            message + f" ({key}: runtime {actual[key]:.4f} vs expected "
                            f"{expected[key]:.4f})")
                        return

            for block_id, item in lowering.items():
                source_bounds = (source_geometry[block_id].get("bounds") or {})
                if not source_bounds:
                    continue
                if item["op"] == "shift":
                    expected = {"x_min": source_bounds["x_min"] + item["dx"],
                                "x_max": source_bounds["x_max"] + item["dx"],
                                "y_min": source_bounds["y_min"] + item["dy"],
                                "y_max": source_bounds["y_max"] + item["dy"]}
                else:
                    mapping = _band_mapping(item["band_start"], item["band_end"],
                                            item["below_offset"], item["above_offset"])
                    if item["axis"] == "y":
                        expected = {"x_min": source_bounds["x_min"], "x_max": source_bounds["x_max"],
                                    "y_min": source_bounds["y_min"] + mapping(source_bounds["y_min"]),
                                    "y_max": source_bounds["y_max"] + mapping(source_bounds["y_max"])}
                    else:
                        expected = {"x_min": source_bounds["x_min"] + mapping(source_bounds["x_min"]),
                                    "x_max": source_bounds["x_max"] + mapping(source_bounds["x_max"]),
                                    "y_min": source_bounds["y_min"], "y_max": source_bounds["y_max"]}
                bbox_equal(runtime_geometry[block_id].get("bounds"), expected,
                           f"Block {block_id} did not reach its expected position")
            for block_id, geometry in source_geometry.items():
                if block_id in lowering or not geometry.get("bounds"):
                    continue
                bbox_equal(runtime_geometry[block_id].get("bounds"), geometry["bounds"],
                           f"Untouched block {block_id} moved")

    # ---- part-level collision gate on the runtime geometry ----
    collision_flagged: list[dict[str, Any]] = []
    preexisting_skipped = 0
    checked_pairs = 0
    runtime_detection = parts_mod.part_index(runtime, params)
    # preflight (stage-7) expects the candidate envelope under ``bounds``
    # (the feature-level preview ships recognition bounds); for part-level
    # candidates the same envelope is the detected sheet bbox.
    runtime_detection["bounds"] = runtime_detection.get("sheet_bbox")
    runtime_parts = runtime_detection["parts"]
    if not errors:
        if len(runtime_parts) != len(detection["parts"]):
            errors.append(
                f"Part structure changed at runtime ({len(runtime_parts)} parts vs "
                f"{len(detection['parts'])} in the source); the edit is rejected")
        else:
            # bugfix: pair source/runtime parts by their contour BLOCK
            # membership, never by sort index. part_index orders parts by
            # (y_min, x_min); a legitimate edit can flip that order (a part
            # moved past another, near-equal y_min swapping), which silently
            # mismatched the gate's pairs — false collisions or missed ones.
            # Block ids are stable across the patch (same block count, 1:1
            # line patch), so membership is the durable identity.
            source_by_blocks: dict[tuple, list[dict[str, Any]]] = {}
            for part in detection["parts"]:
                source_by_blocks.setdefault(tuple(part["contour_block_ids"]), []).append(part)
            matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
            unmatched_runtime: list[dict[str, Any]] = []
            for runtime_part in runtime_parts:
                bucket = source_by_blocks.get(tuple(runtime_part["contour_block_ids"]))
                if bucket:
                    matched.append((bucket.pop(0), runtime_part))
                else:
                    unmatched_runtime.append(runtime_part)
            if unmatched_runtime:
                errors.append(
                    "Part identity changed at runtime for " +
                    ", ".join(part["id"] for part in unmatched_runtime) +
                    " (contour block membership no longer matches the source); "
                    "the edit is rejected")

            count = len(matched)
            for i in range(count):
                for j in range(i + 1, count):
                    src_a_part, rt_a_part = matched[i]
                    src_b_part, rt_b_part = matched[j]
                    src_a = src_a_part["bbox_finished"]
                    src_b = src_b_part["bbox_finished"]
                    rt_a = rt_a_part["bbox_finished"]
                    rt_b = rt_b_part["bbox_finished"]

                    def axis_overlaps(a: dict[str, float], b: dict[str, float]) -> tuple[float, float]:
                        return (min(a["x_max"], b["x_max"]) - max(a["x_min"], b["x_min"]),
                                min(a["y_max"], b["y_max"]) - max(a["y_min"], b["y_min"]))

                    src_ov_x, src_ov_y = axis_overlaps(src_a, src_b)
                    rt_ov_x, rt_ov_y = axis_overlaps(rt_a, rt_b)
                    src_overlap = min(src_ov_x, src_ov_y)
                    rt_overlap = min(rt_ov_x, rt_ov_y)
                    if rt_overlap <= _RUNTIME_TOL and src_overlap <= _RUNTIME_TOL:
                        continue  # disjoint or touching both before and after
                    rel_runtime = (rt_a["x_min"] - rt_b["x_min"], rt_a["y_min"] - rt_b["y_min"])
                    rel_source = (src_a["x_min"] - src_b["x_min"], src_a["y_min"] - src_b["y_min"])
                    arrangement_preserved = (
                        abs(rel_runtime[0] - rel_source[0]) <= _RUNTIME_TOL
                        and abs(rel_runtime[1] - rel_source[1]) <= _RUNTIME_TOL)
                    if src_overlap > _RUNTIME_TOL:
                        # Pre-existing tight packing (tool clearance exceeds the
                        # path gap): allowed while NO axis interferes more than
                        # before — a legitimate stretch shifts the binding edges
                        # of both parts by the same delta, so their overlap stays
                        # identical, while pushing one part INTO another grows at
                        # least one axis.
                        if rt_ov_x <= src_ov_x + _RUNTIME_TOL and rt_ov_y <= src_ov_y + _RUNTIME_TOL:
                            preexisting_skipped += 1
                            continue
                    elif arrangement_preserved:
                        # both boxes moved together as a rigid family
                        preexisting_skipped += 1
                        continue
                    checked_pairs += 1
                    overlap_x = min(rt_a["x_max"], rt_b["x_max"]) - max(rt_a["x_min"], rt_b["x_min"])
                    overlap_y = min(rt_a["y_max"], rt_b["y_max"]) - max(rt_a["y_min"], rt_b["y_min"])
                    collision_flagged.append({
                        "parts": [src_a_part["id"], src_b_part["id"]],
                        "overlap_x": round(overlap_x, 6), "overlap_y": round(overlap_y, 6),
                        "overlap_before": round(max(src_overlap, 0.0), 6),
                        "overlap_after": round(max(rt_overlap, 0.0), 6)})
                    errors.append(
                        f"Parts {src_a_part['id']} and {src_b_part['id']} collide after "
                        f"the edits ({overlap_x:.3f} x {overlap_y:.3f} mm at finished-bbox level "
                        "incl. tool clearance); move them apart or reduce the stretch")

    result_warnings = [
        "برخورد در سطح قطعه با bbox نهایی (شامل قطر ابزار) بررسی شد؛ هم‌پوشانی جزئی تازه رد و "
        "تودرتویی مجاز است؛ دیوار نازک و لبهٔ ورق همچنان نیاز به چشم اپراتور دارد.",
        "خروجی نهایی فقط از مسیر serializer + پست‌پروسسور دیالکت تولید می‌شود؛ این پاسخ پیش‌نمایش است.",
    ] + band_relocation_notes

    result = {
        "preview_only": True,
        "safe_for_execution": False,
        "source_sha256": program.source_sha256,
        "operation": "part_edits",
        "edits_applied": normalized_summary,
        "lowered_block_count": len(lowering),
        "collision_check": {"method": "part_finished_bbox_new_overlaps_rejected",
                            "checked_pairs": checked_pairs,
                            "preexisting_skipped": preexisting_skipped,
                            "flagged": collision_flagged},
        "original": parts_mod._strip_internal(detection),
        "resized": parts_mod._strip_internal(runtime_detection),
        "validation": {"passed": not errors, "errors": errors, "warnings": result_warnings},
        "program_ir": to_dict(transformed),
    }
    return result


def edits_part_program(text: str, request: dict[str, Any], *, filename: str = "<memory>",
                       params: dict[str, Any] | None = None) -> dict[str, Any]:
    """High-level entry: parse ``text`` and preview part-level edits."""
    program = parse_program(text, filename=filename)
    return preview_part_edits(program, request, source_text=text, params=params)
