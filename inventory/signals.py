"""
اتصال خودکار صف مواد روزانه به برنامهٔ نقاشی (Phase 3).

هر تغییری روی ``ProductionTask`` که روی نیاز مواد اثر می‌گذارد باید صف روزانه را
Sync کند: ایجاد تسک، تغییر تاریخ، تغییر کارگر، تغییر مرحله، تغییر مقدار، حذف یا
لغو تسک.

منطق Sync اینجا تکرار نمی‌شود؛ فقط تاریخ‌های تحت تأثیر به سرویس مرکزی
``inventory.services.request_queue_sync_for_tasks`` داده می‌شود.
"""
import logging

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

_PREV_STATE_ATTR = '_queue_previous_state'


def _queue_sync_for(pk_list, extra_dates=()):
    from .services import request_queue_sync_for_tasks
    if not pk_list and not extra_dates:
        return
    try:
        request_queue_sync_for_tasks(list(pk_list), extra_dates=extra_dates)
    except Exception:
        logger.exception('inventory.signals: queue sync request failed')


def _is_queue_relevant(station_name, scheduled_start, painting_stage_id, worker_id):
    return bool(
        station_name == 'paint'
        and scheduled_start is not None
        and painting_stage_id
        and worker_id
    )


@receiver(pre_save, sender='product.ProductionTask')
def _remember_previous_schedule(sender, instance, **kwargs):
    """
    قبل از ذخیره، تاریخ قبلی تسک را نگه می‌دارد تا جابه‌جایی بین دو روز
    (Day 1 → Day 2) هر دو صف را در یک تراکنش واحد اصلاح کند.
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
        # «قبل» باید از وضعیت ذخیره‌شدهٔ قبلی خوانده شود، نه از instance جدید؛
        # وگرنه جابه‌جایی تسک از ایستگاه نقاشی به ایستگاه دیگر، صف روز قبلی را
        # هرگز sync نمی‌کند و نیاز برنامه‌ریزی‌شدهٔ کهنه باقی می‌ماند.
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
    else:
        if _is_queue_relevant(
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

    if instance.station_name != 'paint':
        return
    work_date = _work_date(instance.scheduled_start)
    if not work_date:
        return
    _queue_sync_for([], extra_dates=[work_date])