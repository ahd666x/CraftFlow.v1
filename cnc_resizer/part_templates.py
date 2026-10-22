"""Parametric part-template role capture and geometry matching (بستهٔ ق).

An :class:`~cnc_resizer.models.CncPartTemplate` stores edit ops whose
``feature_id`` values belong to the file the template was recorded on. To
apply the template to a NEW file of the same part, every referenced feature
becomes a *role*: its kind, its center inside the program envelope
(normalized to the envelope size) and its own size. Applying re-matches
those roles against the target file's recognized features:

* the target must have exactly the same total feature count as the source,
* every role must find a candidate of the SAME kind whose scaled expected
  center is within ``tol_center`` and whose scaled expected size is within
  ``tol_size``,
* the nearest candidate must win clearly (a tie is an ambiguity -> reject),
* the mapping must be bijective (a consumed feature cannot serve another).

The matcher only solves the identity problem. Every materialized edit still
goes through the ordinary fail-closed chain (``service.edits_program``:
runtime reparse, depth-repeat groups, collision gate) before anything can
reach a machine file.
"""
from __future__ import annotations

import copy
from typing import Any

# Edit fields that reference a feature of the analyzed source.
ID_FIELDS = ("feature_id", "anchor_feature_id", "second_feature_id")

# Center tolerance: 2 mm floor, 2% of the larger target envelope side.
_TOL_CENTER_FLOOR = 2.0
_TOL_CENTER_RATIO = 0.02
# Size tolerance: 2 mm floor, 5% of the larger expected feature side.
_TOL_SIZE_FLOOR = 2.0
_TOL_SIZE_RATIO = 0.05
# Two candidates closer than this to each other (delta-wise) are a tie.
_TIE_EPSILON = 0.05


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def envelope_of_features(features: list[dict[str, Any]]) -> dict[str, float]:
    """Program envelope over the recognized features that carry bounds."""
    boxes = [f["bounds"] for f in features if isinstance(f.get("bounds"), dict)]
    if not boxes:
        raise ValueError("هیچ ویژگی‌ای مرز هندسی حل‌شده ندارد؛ قالب قطعه قابل ساختن نیست.")
    return {
        "x_min": min(b["x_min"] for b in boxes),
        "y_min": min(b["y_min"] for b in boxes),
        "x_max": max(b["x_max"] for b in boxes),
        "y_max": max(b["y_max"] for b in boxes),
    }


def _with_size(env: dict[str, float]) -> dict[str, float]:
    out = dict(env)
    out["width"] = env["x_max"] - env["x_min"]
    out["height"] = env["y_max"] - env["y_min"]
    return out


def _role_feature_ok(feature: dict[str, Any]) -> bool:
    bounds, center = feature.get("bounds"), feature.get("center")
    return (isinstance(bounds, dict) and isinstance(center, dict)
            and _number(center.get("x")) is not None and _number(center.get("y")) is not None)


def usable_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Features that can serve as role candidates (bounds + center resolved)."""
    return [f for f in features if _role_feature_ok(f)]


def referenced_feature_ids(edits: list[dict[str, Any]]) -> list[str]:
    """Referenced feature ids, first-appearance order, deduplicated."""
    refs: list[str] = []
    for edit in edits:
        for field in ID_FIELDS:
            value = edit.get(field)
            if isinstance(value, str) and value and value not in refs:
                refs.append(value)
    return refs


def capture_roles(features: list[dict[str, Any]], edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build one role per referenced feature (بستهٔ ق: نقش‌های هندسی)."""
    by_id = {f["id"]: f for f in features if isinstance(f.get("id"), str) and _role_feature_ok(f)}
    envelope = _with_size(envelope_of_features(
        [f for f in features if _role_feature_ok(f)]))
    if envelope["width"] <= 0 or envelope["height"] <= 0:
        raise ValueError("مرز برنامهٔ صفر اندازه است؛ قالب قطعه معنا ندارد.")
    roles: list[dict[str, Any]] = []
    for ref in referenced_feature_ids(edits):
        feature = by_id.get(ref)
        if feature is None:
            raise ValueError(f"ویژگی {ref} مرز یا مرکز حل‌شده ندارد و نمی‌تواند نقش قالب قطعه شود.")
        bounds, center = feature["bounds"], feature["center"]
        roles.append({
            "ref": ref,
            "kind": feature.get("kind") or "unknown",
            "rel_cx": (center["x"] - envelope["x_min"]) / envelope["width"],
            "rel_cy": (center["y"] - envelope["y_min"]) / envelope["height"],
            "size": {"width": bounds["x_max"] - bounds["x_min"],
                     "height": bounds["y_max"] - bounds["y_min"]},
        })
    return roles


def match_roles(roles: list[dict[str, Any]], source_bounds: dict[str, Any],
                features: list[dict[str, Any]],
                source_feature_count: int | None = None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Match template roles to target features; returns (mapping, report).

    Raises ``ValueError`` with an operator-readable Persian message on any
    structural mismatch — fail-closed by design.
    """
    usable = [f for f in features if _role_feature_ok(f)]
    if source_feature_count is not None and len(usable) != source_feature_count:
        raise ValueError(
            f"تعداد ویژگی‌های فایل ({len(usable)}) با فایل منبع قالب ({source_feature_count}) تفاوت دارد؛ "
            "این قالب برای همین قطعه ذخیره نشده یا فایل تغییر کرده است.")
    if not usable:
        raise ValueError("فایل هدف هیچ ویژگی حل‌شده‌ای ندارد؛ قالب قابل بارگذاری نیست.")

    target = _with_size(envelope_of_features(usable))
    if target["width"] <= 0 or target["height"] <= 0:
        raise ValueError("مرز برنامهٔ فایل هدف صفر اندازه است؛ قالب قابل بارگذاری نیست.")
    raw_source = {k: _number((source_bounds or {}).get(k)) for k in ("x_min", "y_min", "x_max", "y_max")}
    if any(value is None for value in raw_source.values()):
        raise ValueError("مرز برنامهٔ منبع قالب ناقص است؛ قالب را دوباره ذخیره کنید.")
    source = _with_size(raw_source)
    if source["width"] <= 0 or source["height"] <= 0:
        raise ValueError("مرز برنامهٔ منبع قالب صفر اندازه است؛ قالب معتبر نیست.")
    scale_x = target["width"] / source["width"]
    scale_y = target["height"] / source["height"]
    tol_center = max(_TOL_CENTER_FLOOR, _TOL_CENTER_RATIO * max(target["width"], target["height"]))

    mapping: dict[str, str] = {}
    report: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for index, role in enumerate(roles, start=1):
        expected_cx = target["x_min"] + role["rel_cx"] * target["width"]
        expected_cy = target["y_min"] + role["rel_cy"] * target["height"]
        expected_w = role["size"]["width"] * scale_x
        expected_h = role["size"]["height"] * scale_y
        tol_size_w = max(_TOL_SIZE_FLOOR, _TOL_SIZE_RATIO * max(expected_w, 1e-9))
        tol_size_h = max(_TOL_SIZE_FLOOR, _TOL_SIZE_RATIO * max(expected_h, 1e-9))
        candidates = []
        for feature in usable:
            if feature["id"] in consumed or (feature.get("kind") or "unknown") != role["kind"]:
                continue
            dx = abs(feature["center"]["x"] - expected_cx)
            dy = abs(feature["center"]["y"] - expected_cy)
            dw = abs((feature["bounds"]["x_max"] - feature["bounds"]["x_min"]) - expected_w)
            dh = abs((feature["bounds"]["y_max"] - feature["bounds"]["y_min"]) - expected_h)
            if dx <= tol_center and dy <= tol_center and dw <= tol_size_w and dh <= tol_size_h:
                candidates.append((dx * dx + dy * dy, dx, dy, feature))
        if not candidates:
            nearest_same_kind = min(
                (abs(f["center"]["x"] - expected_cx) + abs(f["center"]["y"] - expected_cy)
                 for f in usable if (f.get("kind") or "unknown") == role["kind"]),
                default=None)
            hint = (f"نزدیک‌ترین ویژگی هم‌نوع {nearest_same_kind:.1f}mm فاصله دارد"
                    if nearest_same_kind is not None else "هیچ ویژگی هم‌نوعی وجود ندارد")
            raise ValueError(
                f"نقش {index} ({role['kind']} — {role['ref']}) در این فایل مطابق پیدا نکرد: {hint}؛ "
                f"تحمل مرکز {tol_center:.1f}mm است.")
        candidates.sort(key=lambda item: item[0])
        best = candidates[0]
        if len(candidates) > 1 and (candidates[1][0] - best[0]) <= _TIE_EPSILON:
            raise ValueError(
                f"نقش {index} ({role['kind']} — {role['ref']}) بین دو ویژگی مبهم است؛ "
                "فایل مبهم است و قالب به‌صورت قطعی بارگذاری نمی‌شود.")
        consumed.add(best[3]["id"])
        mapping[role["ref"]] = best[3]["id"]
        report.append({"ref": role["ref"], "kind": role["kind"], "matched": best[3]["id"],
                       "dx": round(best[1], 3), "dy": round(best[2], 3)})
    return mapping, report


def materialize_edits(payload: list[dict[str, Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Rewrite every referenced id through the mapping (deep copy)."""
    concrete: list[dict[str, Any]] = []
    for index, edit in enumerate(payload):
        if not isinstance(edit, dict):
            raise ValueError(f"ویرایش {index + 1} قالب شیء معتبری نیست.")
        item = copy.deepcopy(edit)
        for field in ID_FIELDS:
            if field in item:
                target = mapping.get(item[field])
                if not target:
                    raise ValueError(f"ویرایش {index + 1}: نقش {item[field]} در تطبیق نیامد.")
                item[field] = target
        concrete.append(item)
    return concrete
