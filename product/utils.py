# product/utils.py - اضافه کردن تابع assign_task_to_worker برای درگ‌اند‌دراپ

import re
import json
import logging
import traceback
from datetime import datetime, time, timedelta
from collections import defaultdict, deque
from copy import copy
from dataclasses import dataclass
from decimal import Decimal

import jdatetime
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q, Max
from django.contrib.auth.models import User
from django.utils import timezone

DEFAULT_TASK_DURATION_MINUTES = 60

from .models import (
    ProductionTask,
    OrderItem,
    ProductionLog,
    ProductionEvent,
    PaintingProcess,
    PaintingStage,
    WorkerProfile,
    PaintingAssignmentRule,
    Material,
    Holiday,
    create_paint_tasks,
)

logger = logging.getLogger(__name__)


def log_production_event(task, event_type, user=None, old_status='', new_status='',
                         old_worker=None, new_worker=None, quantity=0):
    """ثبت متمرکز رویداد تولید - همیشه از این تابع استفاده شود، نه ساخت مستقیم ProductionEvent"""
    try:
        ProductionEvent.objects.create(
            task=task,
            order=task.order,
            order_item=task.order_item,
            station_name=task.station_name,
            event_type=event_type,
            quantity=quantity,
            old_status=old_status,
            new_status=new_status,
            old_worker=old_worker,
            new_worker=new_worker,
            user=user,
        )
    except Exception:
        logger.exception("خطا در ثبت ProductionEvent (نادیده گرفته شد تا جریان اصلی مختل نشود)")


def consume_material_for_task(task):
    """
    برای یک تسک تکمیل‌شده که part و material آن به یک RawMaterial متصل است،
    یک StockMovement مصرفی ثبت می‌کند. Idempotent است: اگر قبلاً برای همین
    تسک ثبت شده باشد، دوباره ثبت نمی‌کند.
    """
    if not task.part or not task.part.material:
        return None
    material = task.part.material
    if not material.raw_material_id:
        return None

    try:
        from inventory.models import StockMovement
        if StockMovement.objects.filter(reference_task=task, movement_type='consumption').exists():
            return None

        qty = Decimal(task.completed_quantity or 0) * Decimal(material.consumption_per_unit or 1)
        if qty <= 0:
            return None

        return StockMovement.objects.create(
            raw_material=material.raw_material,
            movement_type='consumption',
            quantity=qty,
            reference_task=task,
            note=f'مصرف خودکار - تسک #{task.id} ({task.get_station_name_display()})',
            created_by=task.scanned_by,
        )
    except Exception:
        logger.exception("خطا در مصرف خودکار مواد اولیه برای تسک %s", task.pk)
        return None


def _get_task_actual_color_code(task):
    """کد رنگ سفارش برای بخش رنگی تسک را با fallback به رنگ پیش‌فرض محصول می‌یابد."""
    if not task.order_item_id or not task.color_part:
        return None

    color_obj = task.order_item.ordercolor.filter(part=task.color_part).first()
    if color_obj and color_obj.code and color_obj.code != 'nan':
        return str(color_obj.code)

    code = _parse_default_colors(task.order_item.product).get(task.color_part)
    return str(code) if code and code != 'nan' else None


def get_painting_material_requirements_for_task(task):
    """
    فرمول مصرف مواد یک تسک نقاشی: دقیقاً بر اساس (روندِ مرحلهٔ تسک،
    محصول آیتم سفارش، بخش رنگی تسک) از PaintingMaterialRequirement خوانده
    می‌شود. اسلات‌های وابسته به رنگ در این مرحله فقط در حافظه به مادهٔ واقعی
    کد رنگ سفارش تبدیل می‌شوند و مقدار مصرف بدون تغییر باقی می‌ماند.
    """
    from .models import PaintingMaterialRequirement, PaintingColorMaterialVariant

    if task.station_name != 'paint' or not task.painting_stage_id:
        return []
    if not task.order_item_id or not task.order_item.product_id:
        return []
    if not task.color_part:
        return []

    process_id = task.painting_stage.process_id
    product_id = task.order_item.product_id

    requirements = list(
        PaintingMaterialRequirement.objects.filter(
            process_id=process_id,
            product_id=product_id,
            color_part=task.color_part,
        ).select_related('raw_material')
    )

    actual_code = _get_task_actual_color_code(task)
    if not actual_code:
        return requirements

    variant_map = {
        variant.process_material.raw_material_id: variant.raw_material
        for variant in PaintingColorMaterialVariant.objects.filter(
            process_material__process_id=process_id,
            process_material__is_color_variant=True,
            color_code=actual_code,
        ).select_related('raw_material', 'process_material')
    }

    for requirement in requirements:
        real_material = variant_map.get(requirement.raw_material_id)
        if real_material:
            requirement.raw_material = real_material

    return requirements


def get_painting_material_requirements_for_item_colorpart(order_item, color_part):
    """
    فرمول مصرف مواد برای (آیتم سفارش + بخش رنگی)، مستقل از اینکه چند Task/مرحله
    دارد. روند نقاشی از روی کد رنگ واقعی این بخش تعیین می‌شود (با همان منطق
    _get_task_actual_color_code ولی بدون نیاز به task).
    """
    from .models import PaintingMaterialRequirement, PaintingColorMaterialVariant

    if not order_item or not color_part:
        return None, []

    # 1. کد رنگ واقعی این بخش رنگی
    color_obj = order_item.ordercolor.filter(part=color_part).first()
    actual_code = None
    if color_obj and color_obj.code and color_obj.code != 'nan':
        actual_code = str(color_obj.code)
    if not actual_code:
        code = _parse_default_colors(order_item.product).get(color_part)
        actual_code = str(code) if code and code != 'nan' else None
    if not actual_code:
        return None, []

    # 2. روند نقاشی متناظر با این کد رنگ
    process = get_painting_process_for_color(actual_code)
    if not process:
        return None, []

    # 3. فرمول‌های ثبت‌شده برای (روند، محصول، بخش رنگی)
    requirements = list(
        PaintingMaterialRequirement.objects.filter(
            process_id=process.id,
            product_id=order_item.product_id,
            color_part=color_part,
        ).select_related('raw_material')
    )

    # 4. اسلات‌های وابسته به رنگ را به ماده‌ی واقعی تبدیل کن (in-memory، مثل نسخه‌ی task)
    variant_map = {
        v.process_material.raw_material_id: v.raw_material
        for v in PaintingColorMaterialVariant.objects.filter(
            process_material__process_id=process.id,
            process_material__is_color_variant=True,
            color_code=actual_code,
        ).select_related('raw_material', 'process_material')
    }
    for req in requirements:
        real = variant_map.get(req.raw_material_id)
        if real:
            req.raw_material = real

    return process, requirements

    variant_map = {
        variant.process_material.raw_material_id: variant.raw_material
        for variant in PaintingColorMaterialVariant.objects.filter(
            process_material__process_id=process_id,
            process_material__is_color_variant=True,
            color_code=actual_code,
        ).select_related('raw_material', 'process_material')
    }

    for requirement in requirements:
        real_material = variant_map.get(requirement.raw_material_id)
        if real_material:
            requirement.raw_material = real_material

    return requirements


def consume_material_for_paint_task(task):
    """
    برای یک تسک نقاشی (station_name='paint') که painting_stage دارد،
    مواد اولیه مربوط به آن مرحله را از طریق PaintingMaterialRequirement
    مصرف می‌کند. یک تسک نقاشی ممکن است چند ماده اولیه هم‌زمان مصرف کند
    (مثلاً هم رنگ هم تینر)، بنابراین چک idempotency برای هر ماده به‌صورت
    جداگانه (per-task + per-raw_material) انجام می‌شود.

    این تابع فقط برای ایستگاه paint فعال می‌شود؛ برای بقیه ایستگاه‌ها
    از consume_material_for_task فعلی استفاده می‌شود.
    """
    if task.station_name != 'paint' or not task.painting_stage_id:
        return []

    from .models import PaintingMaterialRequirement
    from inventory.models import StockMovement

    created = []
    requirements = get_painting_material_requirements_for_task(task)

    qty = Decimal(task.quantity or 0)

    for req in requirements:
        if StockMovement.objects.filter(
            reference_task=task,
            movement_type='consumption',
            raw_material=req.raw_material,
        ).exists():
            continue

        consumption = qty * req.consumption_per_unit
        if consumption <= 0:
            continue

        created.append(
            StockMovement.objects.create(
                raw_material=req.raw_material,
                movement_type='consumption',
                quantity=consumption,
                reference_task=task,
                note=f'مصرف خودکار نقاشی - تسک #{task.id} ({task.painting_stage.name})',
                created_by=task.scanned_by,
            )
        )

    return created

# ===================================================================
#   کش‌های سراسری
# ===================================================================
_PAINT_PROCESS_CACHE = None
_PAINT_WORKER_CACHE = None


def invalidate_caches():
    global _PAINT_PROCESS_CACHE, _PAINT_WORKER_CACHE
    _PAINT_PROCESS_CACHE = None
    _PAINT_WORKER_CACHE = None
    logger.info("کش‌های نقاشی پاک شدند.")


def _get_process_cache():
    global _PAINT_PROCESS_CACHE
    if _PAINT_PROCESS_CACHE is None:
        _PAINT_PROCESS_CACHE = {}
        for p in PaintingProcess.objects.filter(is_active=True).prefetch_related('stages'):
            for code in (p.color_codes or []):
                _PAINT_PROCESS_CACHE[str(code)] = p
    return _PAINT_PROCESS_CACHE


def _get_worker_cache():
    global _PAINT_WORKER_CACHE
    if _PAINT_WORKER_CACHE is None:
        try:
            _PAINT_WORKER_CACHE = []
            workers_qs = WorkerProfile.objects.filter(
                stage='paint',
                is_available=True
            ).select_related('user')
            for w in workers_qs:
                user = w.user
                skill_priority = {}
                if isinstance(w.skill_priority, dict):
                    skill_priority = {k: int(v) for k, v in w.skill_priority.items() if str(v).isdigit()}
                _PAINT_WORKER_CACHE.append({
                    'user_id': user.id,
                    'skills': list(
                        {token.strip() for s in (w.skills or []) for token in str(s).split() if token.strip()}
                    ) if isinstance(w.skills, list) else [],
                    'skill_priority': skill_priority,
                    'full_name': f"{user.first_name} {user.last_name}".strip() or user.username,
                })
            logger.debug(f"کش کارگران: {len(_PAINT_WORKER_CACHE)} کارگر بارگذاری شد.")
        except Exception as e:
            logger.exception("خطا در بارگذاری کش کارگران")
            _PAINT_WORKER_CACHE = []
    return _PAINT_WORKER_CACHE if isinstance(_PAINT_WORKER_CACHE, list) else []


# ===================================================================
#   جایگزین امن برای eval
# ===================================================================
def _safe_eval(expr, allowed_names):
    try:
        from simpleeval import simple_eval
        return simple_eval(expr, names=allowed_names)
    except ImportError:
        import ast
        import operator
        ops = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.USub: operator.neg,
        }

        def _eval_node(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                if node.id not in allowed_names:
                    raise ValueError(f"متغیر غیرمجاز: {node.id}")
                return allowed_names[node.id]
            if isinstance(node, ast.BinOp):
                return ops[type(node.op)](_eval_node(node.left), _eval_node(node.right))
            if isinstance(node, ast.UnaryOp):
                return ops[type(node.op)](_eval_node(node.operand))
            raise ValueError(f"عبارت غیرمجاز")

        tree = ast.parse(expr, mode='eval')
        return _eval_node(tree.body)


# ===================================================================
#   توابع کمکی عمومی
# ===================================================================

# product/utils.py - بالای فایل در بخش توابع کمکی

def is_working_day(jalali_date):
    """
    تعیین می‌کند که آیا تاریخ جلالی داده‌شده یک روز کاری است.
    روزهای جمعه (weekday==6) و تاریخ‌های موجود در جدول Holiday تعطیل محسوب می‌شوند.
    در jdatetime: شنبه=0, یکشنبه=1, دوشنبه=2, سه‌شنبه=3, چهارشنبه=4, پنجشنبه=5, جمعه=6
    """
    if not isinstance(jalali_date, jdatetime.date):
        jalali_date = jdatetime.date.today()
    # جمعه
    if jalali_date.weekday() == 6:  # جمعه در jdatetime
        return False
    # تعطیلات رسمی
    gregorian = jalali_date.togregorian()
    return not Holiday.objects.filter(date=gregorian).exists()



def get_material_for_color(color_code, mapping=None):
    default_map = {
        '1': 'kham', '2': 'kham', '3': 'kham', '4': 'kham', '5': 'kham',
        '6': 'kham', '7': 'kham', '8': 'balot', '9': 'gerdo', '10': 'gerdo',
        'بتنی': 'botoni', 'جناغی': 'kham', '11': 'balot',
    }
    if not isinstance(mapping, dict):
        mapping = None
    name = mapping.get(color_code) if mapping else default_map.get(str(color_code))
    if name:
        material, _ = Material.objects.get_or_create(name=name, thickness=16)
        return material
    return None


def parse_size_string(size_str):
    if not size_str:
        return {}
    nums = re.findall(r'\d+', size_str)
    if len(nums) == 1:
        return {'length': int(nums[0]), 'width': int(nums[0])}
    if len(nums) >= 2:
        return {'length': int(nums[0]), 'width': int(nums[1])}
    return {}


def apply_size_adjustment(original_length, original_width, diff_dict, rule):
    if not rule or not diff_dict:
        return float(original_length), float(original_width)
    rule = rule.replace('length_diff', str(diff_dict.get('length_diff', 0)))
    rule = rule.replace('width_diff', str(diff_dict.get('width_diff', 0)))
    allowed = {
        "length": float(original_length),
        "width": float(original_width),
        "length_diff": float(diff_dict.get('length_diff', 0)),
        "width_diff": float(diff_dict.get('width_diff', 0)),
    }
    try:
        new_length = _safe_eval(rule, allowed)
        return float(new_length), float(original_width)
    except Exception as e:
        logger.error(f"خطا در اعمال قانون اندازه '{rule}': {e}")
        return float(original_length), float(original_width)


def update_barcode_size(original_barcode, new_length, new_width, order_item_id=None):
    if not original_barcode:
        return original_barcode
    new_size = f"{int(new_length)}x{int(new_width)}"
    pattern = r'\d+x\d+'
    if re.search(pattern, original_barcode):
        barcode = re.sub(pattern, new_size, original_barcode)
    else:
        barcode = f"{original_barcode}.{new_size}"
    if order_item_id:
        barcode = f"{barcode}.item{order_item_id}"
    return barcode


def _parse_default_colors(product):
    default = product.default_colors or {}
    if isinstance(default, str):
        try:
            default = json.loads(default) or {}
        except (json.JSONDecodeError, TypeError):
            default = {}
    return default


def get_unique_color_codes_for_item(item):
    codes = set()
    for c in item.ordercolor.all():
        if c.code and c.code != 'nan':
            codes.add(str(c.code))
    if not codes:
        for code in _parse_default_colors(item.product).values():
            if code and code != 'nan':
                codes.add(str(code))
    return list(codes)


def get_item_color_assignments(item):
    assignments = []
    seen = set()
    order_colors = item.ordercolor.all()
    if order_colors.exists():
        for c in order_colors:
            if c.code and c.code != 'nan':
                key = (c.part, str(c.code))
                if key not in seen:
                    seen.add(key)
                    assignments.append(key)
    else:
        for part, code in _parse_default_colors(item.product).items():
            if code and code != 'nan':
                key = (part, str(code))
                if key not in seen:
                    seen.add(key)
                    assignments.append(key)
    return assignments


def get_painting_process_for_color(color_code):
    if not color_code:
        return None
    cache = _get_process_cache()
    return cache.get(str(color_code).strip())


def parse_jalali_date(date_str):
    if not date_str:
        return jdatetime.date.today()
    try:
        y, m, d = map(int, date_str.split('-'))
        return jdatetime.date(y, m, d)
    except (ValueError, TypeError) as e:
        raise ValueError(f"تاریخ نامعتبر: {date_str}") from e


# ===================================================================
#   استثناهای داخلی برای کنترل جریان زمان‌بندی زنجیره‌ای (cascade)
# ===================================================================
class _CascadeCannotFit(Exception):
    """وقتی درج/جابه‌جایی یک تسک در محدودهٔ روز کاری کارگر جا نمی‌شود."""
    def __init__(self, task, worker_id):
        self.task = task
        self.worker_id = worker_id


class _CascadeTooComplex(Exception):
    """وقتی تعداد جابه‌جایی‌های زنجیره‌ای از سقف ایمنی عبور می‌کند."""
    pass


class _CascadeCrossDayConflict(Exception):
    """وقتی جابه‌جایی باعث نامعتبر شدن مرحله‌ای در روز دیگر می‌شود (خارج از دامنهٔ cascade تک‌روزه)."""
    def __init__(self, task):
        self.task = task


MAX_CASCADE_STEPS = 100


# ===================================================================
#   کمکی‌های زنجیرهٔ مراحل یک قطعه (order_item + color_part)
# ===================================================================
def _get_item_ready_time(order_item_id, color_part, step_order, exclude_task_id=None):
    """
    زمانی که این قطعه/رنگ برای اجرای این مرحله آماده است،
    بر اساس پایان آخرین مرحلهٔ قبلیِ زمان‌بندی‌شده (روی هر کارگری) + زمان خشک‌شدن آن.
    """
    prev_task = ProductionTask.objects.filter(
        order_item_id=order_item_id,
        color_part=color_part,
        station_name='paint',
        step_order__lt=step_order,
        scheduled_end__isnull=False,
    ).exclude(pk=exclude_task_id).order_by('-step_order', '-scheduled_end').first()

    if prev_task:
        drying = prev_task.painting_stage.drying_time_minutes if prev_task.painting_stage else 0
        return prev_task.scheduled_end + timedelta(minutes=drying)
    return None


def _get_item_next_task(order_item_id, color_part, step_order, exclude_task_id=None):
    """نزدیک‌ترین مرحلهٔ بعدیِ زمان‌بندی‌شدهٔ همین قطعه/رنگ (برای بررسی وابستگی رو به جلو)."""
    return ProductionTask.objects.filter(
        order_item_id=order_item_id,
        color_part=color_part,
        station_name='paint',
        step_order__gt=step_order,
        scheduled_start__isnull=False,
    ).exclude(pk=exclude_task_id).select_related('painting_stage', 'order_item').order_by('step_order').first()


# ===================================================================
#   درج + جابه‌جایی دومینویی در برنامهٔ یک روز از یک کارگر
# ===================================================================
def _insert_and_cascade_worker_day(timeline, bounds, new_start, new_end, new_task_marker):
    """
    یک تسک جدید را در timeline درج می‌کند و تسک‌های بعدی را در صورت تداخل به جلو هل می‌دهد.
    وقفهٔ ناهار (bounds['break_start']..bounds['break_end']) کاملاً ثابت است: هیچ تسکی
    نمی‌تواند رویش بیفتد و خودش هرگز جابه‌جا نمی‌شود.
    """
    lunch_start, lunch_end = bounds['break_start'], bounds['break_end']

    # ناهار را از لیست قابل‌شیفت جدا می‌کنیم؛ خودش را جداگانه و بدون تغییر مدیریت می‌کنیم
    items = [list(row) for row in timeline if row[2] is not None]
    items.append([new_start, new_end, new_task_marker])
    items.sort(key=lambda r: r[0])

    pushed = []
    prev_end = bounds['start']

    for row in items:
        s, e, t = row
        old_s, old_e = s, e

        if s < prev_end:
            shift = prev_end - s
            s, e = s + shift, e + shift

        if s < lunch_end and e > lunch_start:
            shift = lunch_end - s
            s, e = s + shift, e + shift

        row[0], row[1] = s, e

        if (s, e) != (old_s, old_e) and t is not new_task_marker:
            pushed.append(t)

        if e > bounds['end']:
            raise _CascadeCannotFit(t if t is not new_task_marker else new_task_marker, None)

        prev_end = e

    # ردیف ثابت ناهار را دوباره اضافه می‌کنیم (بقیهٔ کد انتظار وجودش را دارد)
    items.append([lunch_start, lunch_end, None])
    items.sort(key=lambda r: r[0])

    return items, pushed


def _maybe_enqueue_successor(t, new_end, ref_date, changes, task_objects, queue):
    if not (t.order_item_id and t.color_part):
        return

    drying = t.painting_stage.drying_time_minutes if t.painting_stage else 0
    required_ready = new_end + timedelta(minutes=drying)

    succ = _get_item_next_task(t.order_item_id, t.color_part, t.step_order, exclude_task_id=t.pk)
    if not succ:
        return

    for i, (q_task, q_wid, q_min) in enumerate(queue):
        if q_task.pk == succ.pk:
            if q_min is None or required_ready > q_min:
                queue[i] = (q_task, q_wid, required_ready)
            return

    if succ.pk in changes:
        succ_wid, succ_start, _ = changes[succ.pk]
    else:
        succ_wid, succ_start = succ.assigned_worker_id, succ.scheduled_start

    if succ_start is not None and succ_start >= required_ready:
        return

    if succ_start is not None and succ_start.date() != ref_date:
        raise _CascadeCrossDayConflict(succ)

    task_objects.setdefault(succ.pk, succ)
    queue.append((succ, succ_wid, required_ready))


def _run_cascade_schedule(gregorian_date, initial_entries, exclude_task_ids=None, allow_overtime=False):
    """
    موتور مشترک درج + جابه‌جایی دومینویی برای یک روز مشخص (تقویم میلادی).

    initial_entries: لیستی از (task, worker_id, min_start) که باید (دوباره) در
                     برنامهٔ آن روز درج شوند.
    exclude_task_ids: شناسهٔ تسک‌هایی که نباید از حالت فعلیِ دیتابیس در timeline
                      بارگذاری شوند، چون خودشان در حال درج/جابه‌جایی مجددند.
    allow_overtime: آیا در صورت نیاز زمانبندی خارج از ساعت کاری رسمی مجاز است؟

    خروجی: (changes, task_objects)
        changes: dict از task_id -> (worker_id, start, end)
        task_objects: dict از task_id -> شیء ProductionTask (برای اعمال نهایی توسط فراخواننده)

    این تابع باید داخل transaction.atomic() فراخوانی شود؛ خودش قفل لازم روی
    تسک‌های همان روز می‌گیرد.
    """
    exclude_task_ids = set(exclude_task_ids or [])
    initial_ids = {t.pk for t, _, _ in initial_entries} | exclude_task_ids

    day_tasks = list(
        ProductionTask.objects.select_for_update().filter(
            station_name='paint',
            scheduled_start__date=gregorian_date,
            scheduled_start__isnull=False,
        ).exclude(pk__in=initial_ids).select_related('painting_stage', 'order_item')
    )

    task_objects = {t.pk: t for t, _, _ in initial_entries}
    for t in day_tasks:
        task_objects[t.pk] = t

    timelines = defaultdict(list)
    bounds_cache = {}

    def get_bounds(wid):
        if wid not in bounds_cache:
            bounds_cache[wid] = _worker_day_bounds(gregorian_date, worker_id=wid, allow_overtime=allow_overtime)
        return bounds_cache[wid]

    for t in day_tasks:
        wid = t.assigned_worker_id
        if wid is None:
            continue
        timelines[wid].append([t.scheduled_start, t.scheduled_end, t])

    changes = {}
    queue = deque(initial_entries)

    steps = 0
    while queue:
        steps += 1
        if steps > MAX_CASCADE_STEPS:
            raise _CascadeTooComplex()

        cur_task, wid, min_start = queue.popleft()
        bounds = get_bounds(wid)

        for tl in timelines.values():
            tl[:] = [row for row in tl if not (row[2] is not None and row[2].pk == cur_task.pk)]

        if not any(row[2] is None for row in timelines[wid]):
            timelines[wid].append([bounds['break_start'], bounds['break_end'], None])

        duration = cur_task.painting_stage.duration_minutes if cur_task.painting_stage else DEFAULT_TASK_DURATION_MINUTES

        start_candidate = bounds['start']
        if min_start and min_start > start_candidate:
            start_candidate = min_start
        end_candidate = start_candidate + timedelta(minutes=duration)

        new_items, pushed = _insert_and_cascade_worker_day(
            timelines[wid], bounds, start_candidate, end_candidate, cur_task
        )
        timelines[wid] = new_items

        final_row = next(r for r in new_items if r[2] is cur_task)
        final_start, final_end = final_row[0], final_row[1]
        changes[cur_task.pk] = (wid, final_start, final_end)

        _maybe_enqueue_successor(
            cur_task, final_end, gregorian_date, changes, task_objects, queue
        )

        for pt in pushed:
            row = next(r for r in new_items if r[2] is pt)
            changes[pt.pk] = (wid, row[0], row[1])
            _maybe_enqueue_successor(
                pt, row[1], gregorian_date, changes, task_objects, queue
            )

    return changes, task_objects


# ===================================================================
#   توابع مربوط به کوئری‌های آماده نقاشی
# ===================================================================
def get_painting_ready_items_queryset(search=None, process_id=None):
    has_mon = ProductionLog.objects.filter(order_item=OuterRef('pk'), stage='mon')
    has_paint = ProductionLog.objects.filter(order_item=OuterRef('pk'), stage='paint')
    has_pack = ProductionLog.objects.filter(order_item=OuterRef('pk'), stage='packaging')
    has_unfinished = ProductionTask.objects.filter(
        order_item=OuterRef('pk'), station_name='paint', status__in=['pending', 'waiting']
    )
    has_any = ProductionTask.objects.filter(order_item=OuterRef('pk'), station_name='paint')

    qs = OrderItem.objects.annotate(
        has_mon=Exists(has_mon),
        has_paint=Exists(has_paint),
        has_pack=Exists(has_pack),
        has_unfinished=Exists(has_unfinished),
        has_any=Exists(has_any),
    ).filter(
        has_mon=True,
        has_paint=False,
        has_pack=False,
    ).filter(
        Q(has_unfinished=True) | Q(has_any=False)
    ).distinct().select_related('order', 'product', 'order__customer')

    if search:
        qs = qs.filter(
            Q(order__id__icontains=search) |
            Q(product__name__icontains=search) |
            Q(order__customer__name__icontains=search) |
            Q(order__number__icontains=search) |
            Q(order__user__username__icontains=search) |
            Q(order__user__first_name__icontains=search) |
            Q(order__user__last_name__icontains=search)
        )

    if process_id:
        qs = qs.filter(
            Q(paint_tasks__painting_stage__process_id=process_id) |
            Q(has_any=False)
        ).distinct()
    return qs


def get_unscheduled_ready_items(search=None, process_id=None):
    ready = get_painting_ready_items_queryset(search, process_id)
    has_scheduled = ProductionTask.objects.filter(
        order_item=OuterRef('pk'),
        station_name='paint',
        scheduled_start__isnull=False
    )
    return ready.annotate(_has_scheduled=Exists(has_scheduled)).filter(_has_scheduled=False)


def get_item_paint_preview(item):
    assignments = get_item_color_assignments(item)
    result = {
        'item_id': item.id,
        'color_codes': [f"{p}:{c}" for p, c in assignments],
        'processes': [],
        'unmatched_codes': [],
        'already_has_tasks': item.paint_tasks.filter(station_name='paint').exists(),
        'total_minutes': 0,
        'ok': True,
        'reason': None,
    }
    if not assignments:
        result['ok'] = False
        result['reason'] = 'no_color'
        return result

    for part_name, code in assignments:
        process = get_painting_process_for_color(code)
        if not process:
            result['unmatched_codes'].append(f"{part_name}:{code}")
            continue
        stages = process.stages.all()
        total = sum(s.duration_minutes for s in stages)
        result['processes'].append({
            'part': part_name,
            'color_code': code,
            'process_name': process.name,
            'stage_count': stages.count(),
            'total_minutes': total,
        })
        result['total_minutes'] += total

    if not result['processes']:
        result['ok'] = False
        result['reason'] = 'no_process_match'
    return result


def painting_nav_context():
    return {
        'today': jdatetime.date.today().strftime('%Y/%m/%d'),
        'unscheduled_ready_count': get_unscheduled_ready_items().count(),
    }


# ===================================================================
#   موتور زمان‌بندی نقاشی (Scheduler)
# ===================================================================

def _worker_day_bounds(gregorian_date, worker_id=None, profile=None, allow_overtime=False):
    """مرزهای یک روز کاری (۸:۰۰ تا ۱۶:۳۰ با استراحت ۱۲:۳۰–۱۳:۳۰)"""
    start_time = time(8, 0)
    end_time = time(16, 30)
    break_start_time = time(12, 30)
    break_end_time = time(13, 30)

    if profile is None and worker_id is not None:
        try:
            profile = WorkerProfile.objects.filter(user_id=worker_id).first()
        except Exception:
            logger.warning(f"خطا در خواندن پروفایل کارگر {worker_id}، استفاده از پیش‌فرض", exc_info=True)
            profile = None

    if profile:
        if profile.work_start:
            start_time = profile.work_start
        if profile.work_end and not allow_overtime:
            end_time = profile.work_end
        if profile.break_start:
            break_start_time = profile.break_start
        if profile.break_end:
            break_end_time = profile.break_end

    if allow_overtime:
        end_time = time(23, 59)

    return {
        'start': timezone.make_aware(datetime.combine(gregorian_date, start_time)),
        'end': timezone.make_aware(datetime.combine(gregorian_date, end_time)),
        'break_start': timezone.make_aware(datetime.combine(gregorian_date, break_start_time)),
        'break_end': timezone.make_aware(datetime.combine(gregorian_date, break_end_time)),
    }


# ===================================================================
#   قوانین تخصیص کارگر (PaintingAssignmentRule)
# ===================================================================

def _task_matches_rule(task, rule):
    """
    بررسی می‌کند که آیا یک تسک (ProductionTask) با قانون داده‌شده مطابقت دارد.
    """
    if rule.painting_stage is not None:
        if task.painting_stage_id != rule.painting_stage_id:
            return False

    if rule.process is not None:
        if not task.painting_stage or task.painting_stage.process_id != rule.process_id:
            return False

    if rule.color_codes:
        if not task.order_item or not task.color_part:
            return False

        rule_color_codes = rule.color_codes
        if isinstance(rule_color_codes, str):
            try:
                rule_color_codes = json.loads(rule_color_codes)
            except Exception:
                rule_color_codes = [rule_color_codes]
        if not isinstance(rule_color_codes, list):
            rule_color_codes = []

        if not rule_color_codes:
            return True

        item = task.order_item
        color_code = None

        try:
            color_obj = item.ordercolor.filter(
                part=task.color_part
            ).first()
            if color_obj and color_obj.code and color_obj.code != 'nan':
                color_code = str(color_obj.code)
        except Exception:
            pass

        if color_code is None:
            code = _parse_default_colors(item.product).get(task.color_part)
            if code and code != 'nan':
                color_code = str(code)

        if color_code is None or str(color_code) not in [str(c) for c in rule_color_codes]:
            return False

    return True


class PaintingScheduler:
    def __init__(self, task_ids, target_date, initial_item_cursors=None, assignment_rules=None):
        self.task_ids = list(task_ids) if task_ids else []
        self.target_date = target_date if isinstance(target_date, jdatetime.date) else jdatetime.date.today()
        self.tasks = []
        self.workers = []
        self.worker_schedule = {}
        self.worker_load = {}
        self._loaded = False
        self._all_day_tasks = []
        self.initial_item_cursors = initial_item_cursors or {}

        # تاریخچهٔ کارگر-به-ازای-سفارش
        self._order_worker_history = defaultdict(set)

        # کش استثناهای محصولات (کارگرهای ممنوع)
        self._exclusion_map = {}

        # کش آیتم‌های ممنوعه برای هر کارگر
        self._excluded_items_map = {}

        # قوانین تخصیص کارگر (PaintingAssignmentRule)
        self.assignment_rules = assignment_rules or []

        # ردیابی کارگران تخصیص‌یافته به هر آیتم
        self._item_workers = defaultdict(set)

    def _load(self):
        try:
            logger.debug(f"_load شروع شد برای {self.target_date}")
            gregorian = self.target_date.togregorian()

            self._all_day_tasks = list(
                ProductionTask.objects.filter(
                    station_name='paint',
                    scheduled_start__date=gregorian,
                    scheduled_start__isnull=False,
                )
            )
            logger.debug(f"{len(self._all_day_tasks)} تسک موجود در این روز پیدا شد.")

            if self.task_ids:
                self.tasks = list(
                    ProductionTask.objects.filter(
                        id__in=self.task_ids,
                        station_name='paint',
                        status__in=['pending', 'waiting'],
                        scheduled_start__isnull=True,
                    ).select_related('painting_stage', 'order_item')
                    .order_by('order_item_id', 'step_order')
                )
            else:
                self.tasks = []
            logger.debug(f"{len(self.tasks)} تسک جدید برای زمان‌بندی بارگذاری شد.")

            workers_data = _get_worker_cache()
            if not isinstance(workers_data, list):
                logger.error(f"_get_worker_cache لیست برنگرداند: {type(workers_data)}")
                workers_data = []
            self.workers = [w for w in workers_data if isinstance(w, dict) and 'user_id' in w]
            logger.debug(f"{len(self.workers)} کارگر بارگذاری شدند.")

            self.worker_schedule = {}
            self.worker_load = {}
            for w in self.workers:
                wid = w.get('user_id')
                if wid is not None:
                    self.worker_schedule[wid] = []
                    self.worker_load[wid] = 0

            # محاسبه مرزهای per-worker (با ساعات کاری اختصاصی هر کارگر)
            self._worker_bounds = {}
            worker_ids = [w['user_id'] for w in self.workers if w.get('user_id')]
            profiles_by_id = {}
            if worker_ids:
                profiles = list(
                    WorkerProfile.objects.filter(user_id__in=worker_ids)
                    .prefetch_related('excluded_products', 'excluded_items')
                )
                profiles_by_id = {p.user_id: p for p in profiles}
                for profile in profiles:
                    excluded_ids = set(profile.excluded_products.values_list('id', flat=True))
                    self._exclusion_map[profile.user_id] = excluded_ids
                    self._excluded_items_map[profile.user_id] = set(
                        profile.excluded_items.values_list('id', flat=True)
                    )
            for wid in worker_ids:
                self._worker_bounds[wid] = _worker_day_bounds(
                    gregorian, worker_id=wid, profile=profiles_by_id.get(wid)
                )

            # افزودن بلوک ناهار (per-worker)
            for wid in self.worker_schedule:
                b = self._worker_bounds.get(wid)
                if b:
                    self.worker_schedule[wid].append(
                        (b['break_start'], b['break_end'], None)
                    )

            # پر کردن برنامه از تسک‌های موجود
            for task in self._all_day_tasks:
                wid = task.assigned_worker_id
                if wid in self.worker_schedule:
                    self.worker_schedule[wid].append((task.scheduled_start, task.scheduled_end, task))
                    duration = int((task.scheduled_end - task.scheduled_start).total_seconds() / 60)
                    self.worker_load[wid] = self.worker_load.get(wid, 0) + duration

            for wid in self.worker_schedule:
                self.worker_schedule[wid].sort(key=lambda x: x[0])

            # تاریخچهٔ کارگر-به-ازای-سفارش
            order_ids = {t.order_id for t in self.tasks if t.order_id is not None}
            if order_ids:
                history_qs = (
                    ProductionTask.objects
                    .filter(
                        order_id__in=order_ids,
                        station_name='paint',
                        assigned_worker__isnull=False,
                    )
                    .values_list('order_id', 'assigned_worker_id')
                    .distinct()
                )
                for order_id, worker_id in history_qs:
                    self._order_worker_history[order_id].add(worker_id)

            # NEW: پر کردن _item_workers از تسک‌های موجود در دیتابیس
            # فقط برای آیتم‌های مربوط به بچ فعلی
            item_ids_in_batch = {t.order_item_id for t in self.tasks if t.order_item_id}
            if item_ids_in_batch:
                existing_item_workers = (
                    ProductionTask.objects
                    .filter(
                        order_item_id__in=item_ids_in_batch,
                        station_name='paint',
                        assigned_worker__isnull=False,
                    )
                    .values_list('order_item_id', 'color_part', 'assigned_worker_id')
                )
                for item_id, color_part, wid in existing_item_workers:
                    if item_id and wid:
                        key = (item_id, color_part or '')
                        self._item_workers[key].add(wid)

            self._loaded = True
            logger.info(f"بارگذاری کامل شد: {len(self.tasks)} تسک جدید، {len(self._all_day_tasks)} تسک موجود")

        except Exception as e:
            logger.exception("خطا در _load")
            raise

    def _is_task_excluded_for_worker(self, worker_id, product_id, order_item_id):
        """بررسی ممنوعیت کارگر برای محصول یا آیتم خاص"""
        if product_id and product_id in self._exclusion_map.get(worker_id, set()):
            return True
        if order_item_id and order_item_id in self._excluded_items_map.get(worker_id, set()):
            return True
        return False

    def _find_gap(self, worker_id, duration, bounds, item_ready=None, prefer_early=False):
        if worker_id not in self.worker_schedule:
            return None

        intervals = sorted(self.worker_schedule[worker_id], key=lambda x: x[0])
        candidate = bounds['start']
        if item_ready and item_ready > candidate:
            candidate = item_ready

        best_start = None
        best_gap = timedelta.max

        for start, end, _ in intervals:
            if end <= candidate:
                continue
            if start > candidate:
                gap = start - candidate
                if gap >= timedelta(minutes=duration):
                    if prefer_early:
                        return candidate
                    if gap < best_gap:
                        best_gap = gap
                        best_start = candidate
                    if gap == timedelta(minutes=duration):
                        return candidate
            if end > candidate:
                candidate = end

        if bounds['end'] - candidate >= timedelta(minutes=duration):
            if prefer_early or best_start is None:
                return candidate
            if bounds['end'] - candidate < best_gap:
                best_start = candidate
            return best_start

        return best_start

    def _get_gap_size(self, worker_id, start, bounds):
        if worker_id not in self.worker_schedule:
            return None
        intervals = sorted(self.worker_schedule[worker_id], key=lambda x: x[0])
        for s, e, _ in intervals:
            if s > start:
                return int((s - start).total_seconds() / 60)
        return int((bounds['end'] - start).total_seconds() / 60)

    def _get_worker_load(self, worker_id):
        if worker_id not in self.worker_load:
            total = 0
            for start, end, task in self.worker_schedule.get(worker_id, []):
                if task is not None and start and end:
                    total += int((end - start).total_seconds() / 60)
            self.worker_load[worker_id] = total
        return self.worker_load.get(worker_id, 0)

    def _worker_rule_info(self, task):
        """
        برای یک تسک مشخص، وضعیت قوانین هر کارگر را برمی‌گرداند:
          - excluded: آیا قانون منع‌کننده‌ای برای این تسک وجود دارد؟
          - has_exclusive: آیا کارگر قانون محدودکننده‌ای دارد؟
          - matches_exclusive: آیا تسک با حداقل یک قانون محدودکننده‌ی این کارگر مطابقت دارد؟
        """
        info = {}
        for w in self.workers:
            wid = w.get('user_id')
            if wid is not None:
                info[wid] = {
                    'excluded': False,
                    'has_exclusive': False,
                    'matches_exclusive': False,
                }

        for rule in self.assignment_rules:
            if not rule.is_active:
                continue
            wid = rule.worker.user_id
            if wid not in info:
                continue

            task_matches = _task_matches_rule(task, rule)

            if rule.rule_type == 'exclusion' and task_matches:
                info[wid]['excluded'] = True

            if rule.rule_type == 'exclusive':
                info[wid]['has_exclusive'] = True
                if task_matches:
                    info[wid]['matches_exclusive'] = True

        return info

    def _select_worker(self, task, item_ready):
        try:
            skill = task.painting_stage.required_skill if task.painting_stage else 'painter'
            duration = task.painting_stage.duration_minutes if task.painting_stage else DEFAULT_TASK_DURATION_MINUTES
            is_short_task = duration <= 30

            product = task.order_item.product if task.order_item and task.order_item.product else None
            product_id = product.id if product else None

            item_id = task.order_item_id
            color_part = task.color_part or ''
            item_key = (item_id, color_part)
            item_allowed_workers = None
            if item_id and len(self._item_workers.get(item_key, set())) >= 2:
                item_allowed_workers = self._item_workers.get(item_key, set())

            rule_info = self._worker_rule_info(task)

            candidates = []

            # تمرین اول: با قید کارگران چسبنده (item_allowed_workers)
            for w in self.workers:
                wid = w.get('user_id')
                if wid is None:
                    continue

                if rule_info[wid]['excluded']:
                    continue
                if rule_info[wid]['has_exclusive'] and not rule_info[wid]['matches_exclusive']:
                    continue

                if skill not in (w.get('skills') or []):
                    continue

                if item_allowed_workers is not None and wid not in item_allowed_workers:
                    if not rule_info[wid]['matches_exclusive']:
                        continue

                if self._is_task_excluded_for_worker(wid, product_id, task.order_item_id):
                    continue

                bounds = self._worker_bounds.get(wid)
                if bounds is None:
                    continue

                slot = self._find_gap(wid, duration, bounds, item_ready, prefer_early=is_short_task)
                if slot is None:
                    continue

                load = self._get_worker_load(wid)
                skill_priority = 0
                if skill and isinstance(w.get('skill_priority'), dict):
                    try:
                        skill_priority = int(w['skill_priority'].get(skill, 0))
                    except (TypeError, ValueError):
                        skill_priority = 0
                score = (slot - bounds['start']).total_seconds() / 60 + load * 50 - skill_priority * 50

                existing_worker_ids = self._order_worker_history.get(task.order_id, set())
                if wid in existing_worker_ids:
                    score -= 200

                for rule in self.assignment_rules:
                    if rule.is_active and rule.worker.user_id == wid and _task_matches_rule(task, rule):
                        if rule.rule_type in ('priority', 'exclusive'):
                            score -= rule.priority * 10
                            break

                if is_short_task:
                    gap_size = self._get_gap_size(wid, slot, bounds)
                    if gap_size and gap_size <= duration * 2:
                        score -= 40

                candidates.append((score, wid, slot))

            # اگر هیچ کاندیدی پیدا نشد و قید item_allowed_workers فعال بوده،
            # تمرین دوم بدون این قید (fallback) تا تسک گیر نکند
            if not candidates and item_allowed_workers is not None:
                logger.warning(
                    f"تسک {task.id} با قید کارگر چسبنده ({len(item_allowed_workers)} کارگر) "
                    f"هیچ کارگر مناسبی پیدا نکرد. تلاش مجاز بدون قید..."
                )
                for w in self.workers:
                    wid = w.get('user_id')
                    if wid is None:
                        continue

                    if rule_info[wid]['excluded']:
                        continue
                    if rule_info[wid]['has_exclusive'] and not rule_info[wid]['matches_exclusive']:
                        continue

                    if skill not in (w.get('skills') or []):
                        continue

                    if self._is_task_excluded_for_worker(wid, product_id, task.order_item_id):
                        continue

                    bounds = self._worker_bounds.get(wid)
                    if bounds is None:
                        continue

                    slot = self._find_gap(wid, duration, bounds, item_ready, prefer_early=is_short_task)
                    if slot is None:
                        continue

                    load = self._get_worker_load(wid)
                    skill_priority = 0
                    if skill and isinstance(w.get('skill_priority'), dict):
                        try:
                            skill_priority = int(w['skill_priority'].get(skill, 0))
                        except (TypeError, ValueError):
                            skill_priority = 0
                    score = (slot - bounds['start']).total_seconds() / 60 + load * 50 - skill_priority * 50

                    existing_worker_ids = self._order_worker_history.get(task.order_id, set())
                    if wid in existing_worker_ids:
                        score -= 200

                    for rule in self.assignment_rules:
                        if rule.is_active and rule.worker.user_id == wid and _task_matches_rule(task, rule):
                            if rule.rule_type in ('priority', 'exclusive'):
                                score -= rule.priority * 10
                                break

                    if is_short_task:
                        gap_size = self._get_gap_size(wid, slot, bounds)
                        if gap_size and gap_size <= duration * 2:
                            score -= 40

                    candidates.append((score, wid, slot))

            if not candidates:
                return None, None

            candidates.sort(key=lambda x: x[0])
            selected_score, selected_wid, selected_slot = candidates[0]
            logger.debug(
                f"انتخاب کارگر برای تسک {task.id}: کارگر {selected_wid} با امتیاز {selected_score:.1f}، اسلات {selected_slot}"
            )
            return selected_wid, selected_slot

        except Exception as e:
            logger.exception("خطا در _select_worker")
            raise

    def _assign_task(self, task, worker_id, start):
        try:
            duration = task.painting_stage.duration_minutes if task.painting_stage else DEFAULT_TASK_DURATION_MINUTES
            drying = task.painting_stage.drying_time_minutes if task.painting_stage else 0
            end = start + timedelta(minutes=duration)

            if worker_id not in self.worker_schedule:
                self.worker_schedule[worker_id] = []
            self.worker_schedule[worker_id].append((start, end, task))
            self.worker_schedule[worker_id].sort(key=lambda x: x[0])
            if task is not None:
                self.worker_load[worker_id] = self.worker_load.get(worker_id, 0) + duration

            task._assigned_worker_id = worker_id
            task._scheduled_start = start
            task._scheduled_end = end

            if task.order_id is not None:
                self._order_worker_history[task.order_id].add(worker_id)

            # NEW: بروزرسانی _item_workers در حافظه
            if task.order_item_id:
                key = (task.order_item_id, task.color_part or '')
                self._item_workers[key].add(worker_id)

            return end + timedelta(minutes=drying)

        except Exception as e:
            logger.exception("خطا در _assign_task")
            raise

    def build(self):
        try:
            if not self._loaded:
                self._load()

            if not self.tasks or not self.workers:
                return 0

            self.tasks.sort(key=lambda t: (t.order_item_id, t.step_order))

            scheduled = 0
            item_cursors = dict(self.initial_item_cursors)
            blocked_chains = set()

            for task in self.tasks:
                cursor_key = (task.order_item_id, task.color_part)

                if cursor_key in blocked_chains:
                    continue

                item_ready = item_cursors.get(cursor_key)

                wid, start = self._select_worker(task, item_ready)
                if wid is None:
                    if cursor_key not in blocked_chains:
                        logger.warning(
                            f"مرحله {task.painting_stage.name if task.painting_stage else task.id} "
                            f"برای آیتم {task.order_item_id} (بخش رنگی: {task.color_part}) "
                            f"زمان‌بندی نشد؛ بقیه‌ی مراحل این زنجیره در این اجرا رد می‌شوند."
                        )
                    blocked_chains.add(cursor_key)
                    continue

                next_ready = self._assign_task(task, wid, start)
                item_cursors[cursor_key] = next_ready
                scheduled += 1

            self._remaining_item_cursors = item_cursors

            logger.info(f"{scheduled} تسک از {len(self.tasks)} زمان‌بندی شد.")
            return scheduled

        except Exception as e:
            logger.exception("خطا در build")
            raise

    def apply(self):
        try:
            tasks_to_update = [t for t in self.tasks if hasattr(t, '_assigned_worker_id')]
            if not tasks_to_update:
                return 0

            for task in tasks_to_update:
                task.assigned_worker_id = getattr(task, '_assigned_worker_id', None)
                task.scheduled_start = getattr(task, '_scheduled_start', None)
                task.scheduled_end = getattr(task, '_scheduled_end', None)

            ProductionTask.objects.bulk_update(
                tasks_to_update,
                ['assigned_worker_id', 'scheduled_start', 'scheduled_end']
            )

            logger.info(f"{len(tasks_to_update)} تسک در دیتابیس به‌روزرسانی شد.")
            return len(tasks_to_update)

        except Exception as e:
            logger.exception("خطا در apply")
            raise

    def schedule(self):
        try:
            logger.info(f"شروع schedule برای {self.target_date} با {len(self.task_ids)} تسک")
            self._load()
            count = self.build()
            self.apply()
            logger.info(f"زمان‌بندی کامل شد: {count} تسک")
            return count, getattr(self, '_remaining_item_cursors', {})
        except Exception as e:
            logger.exception("خطا در schedule")
            raise


# ===================================================================
#   توابع عمومی زمان‌بندی
# ===================================================================

def _get_initial_item_cursors(item_ids):
    cursors = {}

    parts = (
        ProductionTask.objects
        .filter(order_item_id__in=item_ids, station_name='paint')
        .values_list('order_item_id', 'color_part')
        .distinct()
    )

    for item_id, color_part in parts:
        last_task = ProductionTask.objects.filter(
            order_item_id=item_id,
            color_part=color_part,
            station_name='paint',
            scheduled_end__isnull=False,
        ).order_by('-scheduled_end').first()

        if last_task:
            drying = last_task.painting_stage.drying_time_minutes if last_task.painting_stage else 0
            cursors[(item_id, color_part)] = last_task.scheduled_end + timedelta(minutes=drying)
        else:
            cursors[(item_id, color_part)] = None

    return cursors


def _get_active_assignment_rules():
    """دریافت لیست قوانین تخصیص فعال برای استفاده در زمان‌بندی."""
    try:
        return list(
            PaintingAssignmentRule.objects.filter(is_active=True)
            .select_related('worker', 'worker__user', 'painting_stage', 'painting_stage__process', 'process')
        )
    except Exception as e:
        logger.error(f"خطا در بارگذاری قوانین تخصیص: {e}")
        return []


def schedule_paint_tasks_for_items(item_ids, target_date, initial_item_cursors=None):
    if not item_ids:
        return 0, {}
    task_ids = list(
        ProductionTask.objects.filter(
            order_item_id__in=item_ids,
            station_name='paint',
            status__in=['pending', 'waiting'],
            scheduled_start__isnull=True,
        ).values_list('id', flat=True)
    )
    if not task_ids:
        return 0, {}

    sched = PaintingScheduler(task_ids, target_date, initial_item_cursors, assignment_rules=_get_active_assignment_rules())
    if not isinstance(sched, PaintingScheduler):
        logger.error(f"خطا: sched از نوع {type(sched)} است، انتظار PaintingScheduler داشتیم.")
        raise TypeError(f"sched باید از نوع PaintingScheduler باشد، اما {type(sched)} دریافت شد.")

    return sched.schedule()


def schedule_paint_items_auto(item_ids, start_date=None, max_days=100, initial_item_cursors=None):
    if not item_ids:
        return 0, None, {}
    if start_date is None:
        start_date = jdatetime.date.today()

    remaining = list(
        ProductionTask.objects.filter(
            order_item_id__in=item_ids,
            station_name='paint',
            status__in=['pending', 'waiting'],
            scheduled_start__isnull=True,
        ).values_list('id', flat=True)
    )

    if not remaining:
        return 0, None, {}

    if initial_item_cursors is None:
        initial_item_cursors = _get_initial_item_cursors(item_ids)

    total = 0
    cur_date = start_date
    safety = 0
    item_cursors = dict(initial_item_cursors)

    while remaining and safety < max_days:
        if not is_working_day(cur_date):
            cur_date += jdatetime.timedelta(days=1)
            safety += 1
            continue

        sched = PaintingScheduler(remaining, cur_date, item_cursors, assignment_rules=_get_active_assignment_rules())
        cnt, new_cursors = sched.schedule()
        total += cnt
        item_cursors.update(new_cursors)

        remaining = list(
            ProductionTask.objects.filter(
                id__in=remaining,
                station_name='paint',
                status__in=['pending', 'waiting'],
                scheduled_start__isnull=True,
            ).values_list('id', flat=True)
        )
        cur_date += jdatetime.timedelta(days=1)
        safety += 1

    if remaining:
        logger.warning(f"{len(remaining)} تسک پس از {max_days} روز همچنان زمان‌بندی نشده‌اند.")

    return total, cur_date - jdatetime.timedelta(days=1), item_cursors


def auto_assign_paint_tasks(target_date=None):
    if target_date is None:
        target_date = jdatetime.date.today()

    if not is_working_day(target_date):
        return 0

    tasks_qs = ProductionTask.objects.filter(
        station_name='paint',
        assigned_worker__isnull=True,
        status__in=['pending', 'waiting'],
        order_item__isnull=False,
        painting_stage__isnull=False,
    )
    if target_date:
        gregorian = target_date.togregorian()
        tasks_qs = tasks_qs.filter(
            Q(scheduled_start__isnull=True) | Q(scheduled_start__date=gregorian)
        )

    task_ids = list(tasks_qs.values_list('id', flat=True))
    if not task_ids:
        return 0

    item_ids = list(tasks_qs.values_list('order_item_id', flat=True).distinct())
    initial_cursors = _get_initial_item_cursors(item_ids)

    sched = PaintingScheduler(task_ids, target_date, initial_cursors, assignment_rules=_get_active_assignment_rules())
    cnt, _ = sched.schedule()
    return cnt


def create_and_schedule_items_for_date(item_ids, target_date=None):
    try:
        if target_date is None:
            target_date = jdatetime.date.today()
            logger.info("target_date None بود، تاریخ امروز جایگزین شد.")

        if not isinstance(target_date, jdatetime.date):
            logger.error(f"target_date از نوع {type(target_date)} است. جایگزین با امروز.")
            target_date = jdatetime.date.today()

        # ایجاد تسک‌های جدید
        items = OrderItem.objects.filter(pk__in=item_ids).prefetch_related('ordercolor', 'paint_tasks')
        created_items = []
        skipped = []
        new_tasks = []

        for item in items:
            if item.paint_tasks.filter(station_name='paint').exists():
                continue
            assignments = get_item_color_assignments(item)
            if not assignments:
                skipped.append({'item_id': item.id, 'reason': 'no_color'})
                continue
            base_step = 0
            any_created = False
            for part_name, color_code in assignments:
                process = get_painting_process_for_color(color_code)
                if not process:
                    continue
                create_paint_tasks(
                    tasks_list=new_tasks,
                    order=item.order,
                    quantity=item.quantity,
                    process=process,
                    base_step=base_step,
                    order_item=item,
                    color_part=part_name,
                )
                base_step += process.stages.count()
                any_created = True
            if any_created:
                created_items.append(item.id)
            else:
                skipped.append({'item_id': item.id, 'reason': 'no_process_match'})

        if new_tasks:
            created_tasks = ProductionTask.objects.bulk_create(new_tasks)
            # خودکارسازی درخواست مواد از انبار بر اساس فرمول ساخت (BOM)
            # برای برنامه‌ریزی آیتم‌های نقاشی
            req_result = auto_create_material_issues(created_tasks, purpose='production')
            if req_result['created']:
                logger.info(
                    f"create_and_schedule_items_for_date: {req_result['created']} "
                    f"درخواست مواد از انبار برای تسک‌های نقاشی جدید ایجاد شد."
                )

        # زمان‌بندی همه تسک‌ها
        all_item_ids = list(
            ProductionTask.objects.filter(
                order_item_id__in=item_ids,
                station_name='paint',
                status__in=['pending', 'waiting'],
                scheduled_start__isnull=True,
            ).values_list('order_item_id', flat=True).distinct()
        )

        if not all_item_ids:
            return {
                'scheduled_count': 0,
                'scheduled_date': target_date,
                'created_items': created_items,
                'skipped': skipped
            }

        initial_cursors = _get_initial_item_cursors(all_item_ids)

        cnt, last_date, _ = schedule_paint_items_auto(
            all_item_ids, target_date, max_days=100, initial_item_cursors=initial_cursors
        )

        return {
            'scheduled_count': cnt,
            'scheduled_date': last_date or target_date,
            'created_items': created_items,
            'skipped': skipped
        }

    except Exception as e:
        logger.exception("خطا در create_and_schedule_items_for_date")
        raise


def repaint_item_ids_for_date(item_ids, target_date):
    items = OrderItem.objects.filter(pk__in=item_ids)
    task_ids = []
    for item in items:
        task_ids.extend(
            ProductionTask.objects.filter(
                order_item=item,
                station_name='paint',
                scheduled_start__date=target_date.togregorian(),
            ).values_list('pk', flat=True)
        )
    if task_ids:
        done = ProductionTask.objects.filter(pk__in=task_ids, status='done').count()
        if done:
            raise ValueError(f'{done} تسک قبلاً انجام شده و نمی‌تواند بازنشانی شود.')
        ProductionTask.objects.filter(pk__in=task_ids).delete()
    return create_and_schedule_items_for_date(item_ids, target_date)


# ===================================================================
#   تابع اختصاصی برای درگ‌اند‌دراپ (تخصیص دستی کارگر به تسک)
# ===================================================================

def assign_task_to_worker(task_id, worker_id, target_date=None, allow_overtime=False):
    """
    تخصیص پویا/دستی یک تسک نقاشی به یک کارگر (درگ‌اند‌دراپ در کانبان).
    در صورت نیاز، تسک‌های دیگر (همان کارگر یا کارگران دیگر که به لحاظ زنجیرهٔ مراحل
    وابسته‌اند) به‌صورت خودکار جابه‌جا می‌شوند تا نه تداخل زمانی رخ دهد و نه ترتیب مراحل
    نقض شود. اگر این کار در محدودهٔ همان روز ممکن نباشد، عملیات کاملاً لغو (rollback)
    و خطای شفاف برگردانده می‌شود.
    """
    try:
        target_date_obj = None
        if target_date is not None:
            if isinstance(target_date, jdatetime.date):
                target_date_obj = target_date.togregorian()
            elif isinstance(target_date, str):
                try:
                    y, m, d = map(int, target_date.split('-'))
                    target_date_obj = jdatetime.date(y, m, d).togregorian()
                except (ValueError, TypeError):
                    target_date_obj = None

        worker_id = int(worker_id)

        # ۱. دریافت تسک
        task = ProductionTask.objects.select_related(
            'painting_stage', 'order_item', 'order_item__product'
        ).filter(pk=task_id, station_name='paint').first()

        if not task:
            return {'ok': False, 'error': 'تسک یافت نشد'}
        if task.status == 'done':
            return {'ok': False, 'error': 'این تسک قبلاً انجام شده است'}

        # ۲. کارگر مقصد
        workers = _get_worker_cache()
        worker_data = next((w for w in workers if w['user_id'] == worker_id), None)
        if not worker_data:
            return {'ok': False, 'error': 'کارگر یافت نشد یا غیرفعال است'}

        # ۳. مهارت
        required_skill = task.painting_stage.required_skill if task.painting_stage else 'painter'
        if required_skill not in (worker_data.get('skills') or []):
            return {
                'ok': False,
                'error': f'کارگر مهارت "{required_skill}" را ندارد. مهارت‌های فعلی: {", ".join((worker_data.get("skills") or []))}'
            }

        # ۳-ب. قوانین تخصیص
        active_rules = _get_active_assignment_rules()
        for rule in active_rules:
            if rule.worker.user_id == worker_id and rule.rule_type == 'exclusion' and _task_matches_rule(task, rule):
                return {'ok': False, 'error': 'این کارگر طبق قانون منع شده است.'}

        has_exclusive = any(
            r.is_active and r.worker.user_id == worker_id and r.rule_type == 'exclusive' for r in active_rules
        )
        if has_exclusive:
            matches_exclusive = any(
                r.is_active and r.worker.user_id == worker_id and r.rule_type == 'exclusive' and _task_matches_rule(task, r)
                for r in active_rules
            )
            if not matches_exclusive:
                return {'ok': False, 'error': 'این کارگر فقط مجاز به تسک‌های مطابق قانونش است.'}

        # ۴. استثناهای محصول/آیتم
        worker_profile = WorkerProfile.objects.filter(user_id=worker_id).first()
        if task.order_item and task.order_item.product:
            if worker_profile and worker_profile.excluded_products.filter(id=task.order_item.product.id).exists():
                return {'ok': False, 'error': f'این کارگر برای محصول "{task.order_item.product.name}" ممنوع است'}
        if task.order_item_id and worker_profile and worker_profile.excluded_items.filter(id=task.order_item_id).exists():
            return {'ok': False, 'error': f'کارگر برای این آیتم (شماره {task.order_item_id}) ممنوع شده است.'}

        # ۵. تعیین روز مرجع
        if task.scheduled_start:
            ref_date = task.scheduled_start.date()
        elif target_date_obj:
            ref_date = target_date_obj
        else:
            ref_date = timezone.localdate()

        ref_jalali = jdatetime.date.fromgregorian(date=ref_date)
        if not is_working_day(ref_jalali):
            return {'ok': False, 'error': 'امروز تعطیل رسمی است'}

        # ۶. زنجیرهٔ درج + جابه‌جایی دومینویی (از موتور مشترک استفاده می‌شود)
        old_worker_id = task.assigned_worker_id
        item_ready = None
        if task.order_item_id and task.color_part:
            item_ready = _get_item_ready_time(
                task.order_item_id, task.color_part, task.step_order, exclude_task_id=task.pk
            )

        initial_entries = [(task, worker_id, item_ready)]
        exclude_ids = [task.pk]

        try:
            with transaction.atomic():
                changes, task_objects = _run_cascade_schedule(
                    ref_date,
                    initial_entries,
                    exclude_task_ids=exclude_ids,
                    allow_overtime=allow_overtime,
                )

                to_update = []
                for pk, (wid, s, e) in changes.items():
                    t = task_objects[pk]
                    t.assigned_worker_id = wid
                    t.scheduled_start = s
                    t.scheduled_end = e
                    to_update.append(t)

                ProductionTask.objects.bulk_update(
                    to_update, ['assigned_worker_id', 'scheduled_start', 'scheduled_end']
                )

                if old_worker_id and old_worker_id != worker_id:
                    source_tasks_count = reschedule_worker_tasks_on_date(
                        old_worker_id,
                        jdatetime.date.fromgregorian(date=ref_date),
                        allow_overtime=allow_overtime,
                    )
                    try:
                        old_worker_obj = User.objects.filter(pk=old_worker_id).first()
                        worker_obj = User.objects.filter(pk=worker_id).first()
                        log_production_event(
                            task=task,
                            event_type='reassigned',
                            user=None,
                            old_worker=old_worker_obj,
                            new_worker=worker_obj,
                        )
                    except Exception:
                        logger.exception("خطا در ثبت رویداد reassigned")
                else:
                    source_tasks_count = 0

                dest_tasks_count = reschedule_worker_tasks_on_date(
                    worker_id,
                    jdatetime.date.fromgregorian(date=ref_date),
                    allow_overtime=allow_overtime,
                )

        except _CascadeCannotFit:
            if not allow_overtime:
                try:
                    with transaction.atomic():
                        _run_cascade_schedule(
                            ref_date,
                            initial_entries,
                            exclude_task_ids=exclude_ids,
                            allow_overtime=True,
                        )
                except (_CascadeCannotFit, _CascadeTooComplex, _CascadeCrossDayConflict):
                    return {
                        'ok': False,
                        'error': 'زمان‌بندی این تسک در این روز ممکن نیست.',
                    }

                return {
                    'ok': False,
                    'requires_overtime_confirmation': True,
                    'error': 'این تغییر باعث خروج از ساعت کاری می‌شود. آیا ادامه می‌دهید؟',
                }
            else:
                return {
                    'ok': False,
                    'error': 'زمان‌بندی این تسک حتی با اضافه‌کاری هم ممکن نیست.',
                }

        except _CascadeTooComplex:
            return {
                'ok': False,
                'error': 'زنجیرهٔ جابه‌جایی‌های ناشی از این تغییر خیلی پیچیده است. لطفاً به‌صورت مرحله‌ای جابه‌جا کنید.',
            }

        except _CascadeCrossDayConflict as e:
            return {
                'ok': False,
                'error': f'این جابه‌جایی باعث تداخل با مرحلهٔ بعدیِ همین قطعه در روز دیگری می‌شود (تسک {e.task.id}).',
            }

        final_start, final_end = changes[task.pk][1], changes[task.pk][2]
        return {
            'ok': True,
            'scheduled_start': final_start.strftime('%H:%M'),
            'scheduled_end': final_end.strftime('%H:%M'),
            'shifted_tasks_count': len(changes) - 1 + source_tasks_count + dest_tasks_count,
        }

    except Exception as e:
        logger.exception("خطا در assign_task_to_worker")
        return {'ok': False, 'error': f'خطای داخلی: {str(e)}'}


def reschedule_worker_tasks_on_date(worker_id, target_date, allow_overtime=False):
    """
    پس از ویرایش دستی، تمام تسک‌های یک کارگر در یک روز را با همان موتور cascade
    (درج + جابه‌جایی دومینویی) مورد استفاده در assign_task_to_worker، دوباره
    مرتب‌سازی و زمان‌بندی می‌کند. ترتیب مراحل حفظ می‌شود و در صورت نیاز، تسک‌های
    مرتبطِ همان قطعه روی کارگران دیگر نیز به‌صورت زنجیره‌ای هماهنگ می‌شوند.
    عملیات کاملاً اتمیک است: در صورت شکست، هیچ تغییری اعمال نمی‌شود.
    """
    try:
        if not isinstance(target_date, jdatetime.date):
            return 0

        if not is_working_day(target_date):
            return 0

        worker_id = int(worker_id)
        gregorian = target_date.togregorian()

        tasks = list(
            ProductionTask.objects.filter(
                station_name='paint',
                assigned_worker_id=worker_id,
                scheduled_start__date=gregorian,
                status__in=['pending', 'waiting'],
            ).select_related('painting_stage', 'order_item').order_by('order_item_id', 'step_order')
        )

        if not tasks:
            return 0

        try:
            with transaction.atomic():
                local_ready = {}
                initial_entries = []
                default_bounds = _worker_day_bounds(
                    gregorian, worker_id=worker_id, allow_overtime=allow_overtime
                )
                start_of_day = default_bounds['start']

                for task in tasks:
                    cursor_key = (task.order_item_id, task.color_part)

                    db_ready = _get_item_ready_time(
                        task.order_item_id, task.color_part, task.step_order, exclude_task_id=task.id
                    )
                    local = local_ready.get(cursor_key)
                    candidates = [v for v in [db_ready, local] if v is not None]
                    item_ready = max(candidates) if candidates else None

                    initial_entries.append((task, worker_id, item_ready))

                    duration = task.painting_stage.duration_minutes if task.painting_stage else DEFAULT_TASK_DURATION_MINUTES
                    drying = task.painting_stage.drying_time_minutes if task.painting_stage else 0

                    effective_ready = item_ready if (item_ready and item_ready > start_of_day) else start_of_day
                    local_ready[cursor_key] = effective_ready + timedelta(minutes=duration + drying)

                # مرتب‌سازی بر اساس زمان آمادگی واقعی
                initial_entries.sort(key=lambda entry: entry[2] or start_of_day)

                changes, task_objects = _run_cascade_schedule(
                    gregorian, initial_entries, exclude_task_ids=[t.id for t in tasks],
                    allow_overtime=allow_overtime,
                )

                to_update = []
                for pk, (wid, s, e) in changes.items():
                    t = task_objects.get(pk)
                    if not t:
                        continue

                    if (t.assigned_worker_id, t.scheduled_start, t.scheduled_end) != (wid, s, e):
                        t.assigned_worker_id = wid
                        t.scheduled_start = s
                        t.scheduled_end = e
                        to_update.append(t)

                if to_update:
                    ProductionTask.objects.bulk_update(
                        to_update,
                        ['assigned_worker_id', 'scheduled_start', 'scheduled_end']
                    )

                return len(to_update)

        except (_CascadeCannotFit, _CascadeTooComplex, _CascadeCrossDayConflict) as e:
            logger.warning(f"reschedule_worker_tasks_on_date: cascade ناموفق برای کارگر {worker_id} در {target_date}: {e}")
            return 0

    except Exception:
        logger.exception("خطا در reschedule_worker_tasks_on_date")
        return 0


# ===================================================================
#   پیشرفت تسک‌های یک آیتم در یک ایستگاه
# ===================================================================

def get_item_task_progress_for_station(order_item_id, station_name):
    tasks = ProductionTask.objects.filter(
        order_item_id=order_item_id,
        station_name=station_name
    ).select_related('part', 'order_item__product__category').order_by('step_order')

    result = []
    for task in tasks:
        if task.part:
            part_name = task.part.name or task.part.f2 or task.part.f3 or 'قطعه'
            category_name = ''
            product_name = ''
            if task.order_item and task.order_item.product:
                product_name = task.order_item.product.name
                if task.order_item.product.category:
                    category_name = task.order_item.product.category.name
            elif task.part.pname:
                product_name = task.part.pname

            if category_name and product_name:
                label = f"{part_name} ({category_name} - {product_name})"
            elif category_name:
                label = f"{part_name} ({category_name})"
            elif product_name:
                label = f"{part_name} ({product_name})"
            else:
                label = part_name
        else:
            label = "—"

        result.append({
            'task_id': task.id,
            'order_item_id': task.order_item_id,
            'label': label,
            'download_label': (task.part.f3 or '') if task.part else '',
            'completed_quantity': task.completed_quantity,
            'quantity': task.quantity,
            'status': task.status,
        })
    return result


# ===================================================================
#   سیگنال‌ها
# ===================================================================
from django.db.models.signals import post_save, post_delete


def _invalidate_on_change(sender, **kwargs):
    invalidate_caches()


post_save.connect(_invalidate_on_change, sender=PaintingProcess)
post_delete.connect(_invalidate_on_change, sender=PaintingProcess)
post_save.connect(_invalidate_on_change, sender=WorkerProfile)
post_delete.connect(_invalidate_on_change, sender=WorkerProfile)


# ===================================================================
#   خودکارسازی درخواست مواد اولیه از BOM
#   وقتی تسک‌های نقاشی برنامه‌ریزی می‌شوند، درخواست مواد انباری
#   به‌صورت خودکار ساخته می‌شود تا کارگران انبار بتوانند تحویل دهند.
# ===================================================================

def auto_create_material_issues(tasks, requested_by=None, purpose='production'):
    """
    برای تسک‌های غیرنقاشی: مثل قبل، per-task.
    برای تسک‌های نقاشی: به‌جای per-task، به ازای هر (order_item, color_part)
    یکتا در لیست ورودی، فقط یک‌بار درخواست ساخته می‌شود (اگر از قبل نبوده).
    """
    from inventory.models import MaterialIssue
    from decimal import Decimal

    created_count = 0
    skipped_count = 0

    paint_tasks = [t for t in tasks if t.station_name == 'paint' and t.order_item_id]
    other_tasks = [t for t in tasks if t.station_name != 'paint']

    # ---------- شاخه‌ی غیرنقاشی: بدون تغییر (per-task) ----------
    existing_task_based = set(
        MaterialIssue.objects.exclude(status='cancelled')
        .filter(task__isnull=False)
        .values_list('task_id', 'raw_material_id')
    )
    for task in other_tasks:
        try:
            from inventory.views import _task_material_requirements
            rows = _task_material_requirements(task)
        except Exception:
            rows = []
        for raw_material, quantity in rows:
            if not raw_material or not quantity or quantity <= 0:
                continue
            key = (task.id, raw_material.id)
            if key in existing_task_based:
                skipped_count += 1
                continue
            MaterialIssue.objects.create(
                task=task, raw_material=raw_material,
                requested_quantity=quantity, purpose=purpose, status='requested',
                requested_by=requested_by,
                note='درخواست خودکار از برنامه تولید',
            )
            existing_task_based.add(key)
            created_count += 1

    # ---------- شاخه‌ی نقاشی: به سطح (order_item, color_part) تقلیل بده ----------
    seen_item_colorparts = set()
    existing_paint = set(
        MaterialIssue.objects.exclude(status='cancelled')
        .filter(order_item__isnull=False, task__isnull=True)
        .values_list('order_item_id', 'color_part', 'raw_material_id')
    )

    for task in paint_tasks:
        key = (task.order_item_id, task.color_part or '')
        if key in seen_item_colorparts:
            continue
        seen_item_colorparts.add(key)

        item = task.order_item
        process, requirements = get_painting_material_requirements_for_item_colorpart(
            item, task.color_part
        )
        if not process or not requirements:
            continue

        for req in requirements:
            quantity = Decimal(item.quantity) * req.consumption_per_unit
            if quantity <= 0:
                continue
            dedup_key = (item.id, task.color_part or '', req.raw_material.id)
            if dedup_key in existing_paint:
                skipped_count += 1
                continue

            MaterialIssue.objects.create(
                task=None,
                order_item=item,
                color_part=task.color_part or '',
                painting_process=process,
                raw_material=req.raw_material,
                requested_quantity=quantity,
                purpose=purpose,
                status='requested',
                requested_by=requested_by,
                note=(
                    f'نیاز نقاشی: سفارش {item.order_id} / آیتم {item.id} / '
                    f'{item.product.name} / {task.color_part} / روند {process.name}'
                ),
            )
            existing_paint.add(dedup_key)
            created_count += 1

    return {'created': created_count, 'skipped': skipped_count, 'total_raw': None}