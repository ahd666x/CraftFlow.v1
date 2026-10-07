"""
اتصال خودکار صف مواد روزانه به برنامهٔ تولید.

هر تغییری که روی نیاز مواد اثر بگذارد باید صف روزانه را Sync کند. منبع
نیاز سه چیز است و هر سه اینجا پوشش داده شده‌اند:

    * ``ProductionTask`` با ایستگاه ``paint``       — نیاز نقاشی
    * ``ProductionTask`` با هر ایستگاه دیگر        — نیاز قطعه/ماده
    * ``ProductionDefect`` با مادهٔ جایگزین         — نیاز جبران خرابی

منطق Sync اینجا تکرار نمی‌شود؛ فقط تاریخ‌های تحت تأثیر به سرویس مرکزی
``inventory.services`` داده می‌شود تا همهٔ نوشتن‌ها از یک مسیر بگذرد.
"""
import logging

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

_PREV_STATE_ATTR = '_queue_previous_state'

# وضعیت‌هایی که یعنی خرابی هنوز «نیاز جبران» فعالی در صف روزانه دارد.
_REWORK_STATUSES = ('reported', 'material_requested')


def _queue_sync_for(pk_list, extra_dates=()):
    from .services import request_queue_sync_for_tasks
    if not pk_list and not extra_dates:
        return
    try:
        request_queue_sync_for_tasks(list(pk_list), extra_dates=extra_dates)
    except Exception:
        logger.exception('inventory.signals: queue sync request failed')


def _is_queue_relevant(station_name, scheduled_start, painting_stage_id,
                       worker_id):
    """
    آیا این تسک می‌تواند به صف روزانه نیاز بدهد؟

    ایستگاه نقاشی بدون مرحله نیازی ندارد، چون مقدار از فرمول مصرف همان مرحله
    می‌آید؛ سایر ایستگاه‌ها مرحله لازم ندارند و نیازشان از قطعهٔ متصل به ماده
    می‌آید. شرط مشترک هر دو، تاریخ و کارگر است.
    """
    if scheduled_start is None or not worker_id:
        return False
    if station_name == 'paint':
        return bool(painting_stage_id)
    return True


@receiver(pre_save, sender='product.ProductionTask')
def _remember_previous_schedule(sender, instance, **kwargs):
    """
    قبل از ذخیره، وضعیت قبلی تسک را نگه می‌دارد.

    لازم است تا جابه‌جایی بین دو روز (روز ۱ به روز ۲) یا جابه‌جایی از ایستگاه
    نقاشی به ایستگاه دیگر، هر دو صف را در یک تراکنش واحد اصلاح کند؛ وگرنه نیاز
    برنامه‌ریزی‌شدهٔ کهنه در روز قبلی باقی می‌ماند.
    """
    previous_date = None
    previous_worker = None
    previous_stage = None
    previous_station = None
    if instance.pk:
        row = (
            sender.objects.filter(pk=instance.pk)
            .values_list(
                'scheduled_start', 'assigned_worker_id',
                'painting_stage_id', 'station_name',
            )
            .first()
        )
        if row:
            previous_date, previous_worker, previous_stage, previous_station = row

    from .services import _work_date

    setattr(instance, _PREV_STATE_ATTR, {
        'date': _work_date(previous_date),
        'work_date': _work_date(instance.scheduled_start),
        # «قبل» باید از وضعیت ذخیره‌شدهٔ قبلی خوانده شود، نه از instance جدید.
        'relevant_before': _is_queue_relevant(
            previous_station, previous_date, previous_stage, previous_worker),
        'relevant_after': _is_queue_relevant(
            instance.station_name, instance.scheduled_start,
            instance.painting_stage_id, instance.assigned_worker_id),
    })


@receiver(post_save, sender='product.ProductionTask')
def _sync_after_task_save(sender, instance, created, **kwargs):
    state = getattr(instance, _PREV_STATE_ATTR, None)
    dates = set()
    if state:
        if state.get('relevant_before') and state.get('date'):
            dates.add(state['date'])
        if state.get('relevant_after') and state.get('work_date'):
            dates.add(state['work_date'])
    elif _is_queue_relevant(
        instance.station_name, instance.scheduled_start,
        instance.painting_stage_id, instance.assigned_worker_id,
    ):
        from .services import _work_date
        work_date = _work_date(instance.scheduled_start)
        if work_date:
            dates.add(work_date)

    if not dates:
        return
    _queue_sync_for([instance.pk], extra_dates=dates)


@receiver(post_delete, sender='product.ProductionTask')
def _sync_after_task_delete(sender, instance, **kwargs):
    from .services import _work_date

    work_date = _work_date(instance.scheduled_start)
    if not work_date:
        return
    _queue_sync_for([], extra_dates=[work_date])


# ---------------------------------------------------------------------------
# نیاز جبران خرابی
# ---------------------------------------------------------------------------

def _defect_is_queue_relevant(status, material_work_date):
    """خرابی وقتی نیاز فعال دارد که هنوز جبرانش نشده و تاریخ کاری دارد."""
    return bool(status in _REWORK_STATUSES and material_work_date)


@receiver(pre_save, sender='product.ProductionDefect')
def _remember_previous_defect(sender, instance, **kwargs):
    """
    وضعیت قبلی خرابی را نگه می‌دارد تا روزِ قبلی و روزِ جدید هر دو Sync شوند —
    مثلاً وقتی انباردار مادهٔ جایگزین یا تاریخ کاری را اصلاح می‌کند.
    """
    previous_status = None
    previous_date = None
    if instance.pk:
        row = (
            sender.objects.filter(pk=instance.pk)
            .values_list('status', 'material_work_date')
            .first()
        )
        if row:
            previous_status, previous_date = row

    setattr(instance, _PREV_STATE_ATTR, {
        'relevant_before': _defect_is_queue_relevant(previous_status, previous_date),
        'relevant_after': _defect_is_queue_relevant(
            instance.status, instance.material_work_date),
        'date_before': previous_date,
        'date_after': instance.material_work_date,
    })


@receiver(post_save, sender='product.ProductionDefect')
def _sync_after_defect_save(sender, instance, created, **kwargs):
    state = getattr(instance, _PREV_STATE_ATTR, None) or {}
    dates = set()
    if state.get('relevant_before') and state.get('date_before'):
        dates.add(state['date_before'])
    if state.get('relevant_after') and state.get('date_after'):
        dates.add(state['date_after'])

    if not dates:
        return
    # تاریخ‌ها کافی‌اند: نیاز جبران از تسک نمی‌آید و در همان روزِ خودش مشتق
    # می‌شود، پس نیازی به بازسازی از روی تسک‌ها نیست.
    _queue_sync_for([], extra_dates=dates)


@receiver(post_delete, sender='product.ProductionDefect')
def _sync_after_defect_delete(sender, instance, **kwargs):
    if not _defect_is_queue_relevant(
        instance.status, instance.material_work_date):
        return
    _queue_sync_for([], extra_dates=[instance.material_work_date])