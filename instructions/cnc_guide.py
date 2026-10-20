"""موتور «راهنمای خودکار اپراتور CNC» — فاز ۲ سیستم دستورالعمل‌ها.

راه‌اندازی ثابت ماشین (پارکینگ ابزار، روند آفست، نقاط میخ/فیکس) را برای
سه نقطه‌ی مصرف آماده می‌کند:

* صفحه‌ی اسکن ایستگاه CNC (پس از اسکن قطعه) — با ارزیابی ممنوع/امنِ هر
  نقطه‌ی فیکس برای همان قطعه؛
* صفحه‌ی ``/cnc/`` — همان داده به‌عنوان JSON برای لایه‌ی نقاط فیکس روی
  نقشه‌ی CAD؛ ارزیابی سمت کلاینت با هندسه‌ی زنده انجام می‌شود؛
* سند «دستور تولید» محصول — جدول ابزار + نقشه‌ی میز + روند آفست به‌عنوان
  بخش خودکار ایستگاه CNC.

ارزیابی ایمنی میخ‌کوبی: مبدأ برنامه (G54 X0Y0) روی میز جایی است که
``CncMachineSetup.origin_x/origin_y`` می‌گوید؛ پس هر نقطه‌ی فیکس با مختصات
میز ``(p.x, p.y)`` در مختصات برنامه ``(p.x - origin_x, p.y - origin_y)``
است. کادر مسیر برش قطعه از تحلیل G-code (اجتماع باند ویژگی‌ها) به‌دست
می‌آید و با ``safety_margin`` بزرگ می‌شود؛ هر نقطه‌ای که با شعاع ممنوع خود
با این کادر تماس داشته باشد «ممنوع» است، وگرنه «امن».
"""
from .models import CncMachineSetup


def get_active_setup():
    """ماشین فعال (اولین رکورد فعال)؛ None یعنی راهنمای خودکار خاموش است."""
    return (CncMachineSetup.objects
            .filter(active=True)
            .prefetch_related('tool_slots', 'fixture_points')
            .first())


def serialize_setup(setup):
    """دیکشنری کامل راه‌اندازی برای JSON (اسکن و /cnc/)."""
    if setup is None:
        return None
    return {
        'name': setup.name,
        'table': {'width': setup.table_width, 'height': setup.table_height},
        'origin': {'x': setup.origin_x, 'y': setup.origin_y},
        'safety_margin': setup.safety_margin,
        'tools': [
            {
                'slot': row.slot,
                'label': f"T{row.slot}",
                'type': row.cutter_type,
                'type_label': row.get_cutter_type_display(),
                'diameter': float(row.diameter) if row.diameter is not None else None,
                'diameter_display': row.diameter_display,
                'description': row.description,
            }
            for row in setup.tool_slots.all()
        ],
        'points': serialize_points(setup),
        'offset_procedure': [line.strip() for line in setup.offset_procedure.splitlines() if line.strip()],
        'notes': [line.strip() for line in setup.notes.splitlines() if line.strip()],
    }


def serialize_points(setup):
    """نقاط فیکس فعال با برچسب نوع (وضعیت بعداً ارزیابی می‌شود)."""
    return [
        {
            'label': point.label,
            'kind': point.kind,
            'kind_label': point.get_kind_display(),
            'x': point.x,
            'y': point.y,
            'radius': point.radius,
            'status': None,  # None = هنوز ارزیابی نشده
        }
        for point in setup.fixture_points.filter(active=True)
    ]


def cut_bounds_from_text(text):
    """کادر مسیر برش از تحلیل G-code؛ ``None`` یعنی تحلیل ممکن نشد.

    اجتماع باند همه‌ی ویژگی‌های شناسایی‌شده (کانتور بیرونی، شیار، سوراخ)
    است — محافظه‌کارانه و مستقل از تشخیص نوع هر ویژگی.
    """
    from cnc_resizer import core

    if not text:
        return None
    try:
        result = core.analyze(text)
    except core.CncError:
        return None
    features = result.get('features') or []
    xs0, ys0, xs1, ys1 = [], [], [], []
    for feature in features:
        bounds = feature.get('bounds') or {}
        if not bounds:
            continue
        xs0.append(bounds['x0']); ys0.append(bounds['y0'])
        xs1.append(bounds['x1']); ys1.append(bounds['y1'])
    if not xs0:
        return None
    return {
        'x0': min(xs0), 'y0': min(ys0),
        'x1': max(xs1), 'y1': max(ys1),
    }


def assess_points(setup, bounds):
    """وضعیت ممنوع/امن هر نقطه‌ی فیکس فعال برای کادر برش داده‌شده.

    ``bounds`` مختصات برنامه است؛ ``None`` یعنی بدون ارزیابی (status=None).
    """
    points = serialize_points(setup)
    if bounds is None:
        return points
    margin = setup.safety_margin
    for point in points:
        px = point['x'] - setup.origin_x
        py = point['y'] - setup.origin_y
        reach = point['radius'] + margin
        forbidden = (
            bounds['x0'] - reach <= px <= bounds['x1'] + reach
            and bounds['y0'] - reach <= py <= bounds['y1'] + reach
        )
        point['status'] = 'forbidden' if forbidden else 'safe'
    return points


def part_cnc_guide(part, user=None):
    """راهنمای کامل ایستگاه CNC برای یک قطعه (payload صفحه‌ی اسکن).

    اگر فایل قطعه در دسترس و قابل تحلیل باشد، وضعیت هر نقطه‌ی فیکس هم
    محاسبه می‌شود؛ در غیر این صورت نقاط بدون وضعیت برمی‌گردند (راهنمای
    عمومی ماشین). هیچ‌وقت exception پس نمی‌دهد — خرابی فایل یعنی افت
    ارزیابی، نه خرابی صفحه‌ی اسکن.
    """
    setup = get_active_setup()
    if setup is None:
        return None
    payload = serialize_setup(setup)
    bounds = None
    try:
        from cnc_resizer.services import resolve_cnc_file
        from cnc_resizer import core

        record = resolve_cnc_file(part, user)
        with open(record.path, 'r', encoding='utf-8-sig') as stream:
            text = stream.read(core.MAX_BYTES + 1)
        bounds = cut_bounds_from_text(text)
    except Exception:
        bounds = None
    payload['part_assessed'] = bounds is not None
    payload['points'] = assess_points(setup, bounds)
    return payload
