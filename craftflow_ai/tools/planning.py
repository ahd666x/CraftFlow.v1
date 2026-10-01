"""برنامه‌ریزی و تحویل سفارش (فقط-خواندنی).

ابزارهای این ماژول از داده‌های زمان‌بندی واقعی (``scheduled_start``/``scheduled_end``)
و منطق روز کاری CraftFlow (``product.utils.is_working_day``) استفاده می‌کنند.
"""
import jdatetime
from django.db.models import Count

from product.models import ProductionTask
from product.utils import is_working_day

from craftflow_ai.services import queries
from craftflow_ai.tools.registry import Tool, fail, ok


def get_order_timeline(order_id):
    """ترتیب مراحل یک سفارش بر اساس step_order واقعی."""
    from product.models import Order

    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        return fail('ORDER_NOT_FOUND', f'سفارشی با شناسهٔ {order_id} یافت نشد.', order_id=order_id)
    except (ValueError, TypeError):
        return fail('ORDER_NOT_FOUND', f'سفارشی با شناسهٔ {order_id} یافت نشد.', order_id=order_id)

    labels = queries.station_map()
    tasks = list(
        order.tasks
        .select_related('part', 'assigned_worker', 'painting_stage__process')
        .order_by('step_order')
    )
    steps = [{
        'step_order': t.step_order,
        'stage': t.station_name,
        'stage_label': t.get_station_name_display(),
        'status': t.status,
        'status_label': t.get_status_display(),
        'quantity': t.quantity,
        'completed_quantity': t.completed_quantity,
        'part': t.part.name if t.part_id else None,
        'painting_stage': t.painting_stage.name if t.painting_stage_id else None,
        'scheduled_start': t.scheduled_start.isoformat() if t.scheduled_start else None,
        'scheduled_end': t.scheduled_end.isoformat() if t.scheduled_end else None,
        'assigned_worker': (
            (t.assigned_worker.get_full_name() or t.assigned_worker.username)
            if t.assigned_worker_id else None
        ),
        'is_done': t.status == queries.STATUS_DONE,
    } for t in tasks]

    last_done = [s for s in steps if s['is_done']]
    return ok({
        'order_id': order.id,
        'steps': steps,
        'total_steps': len(steps),
        'completed_steps': len(last_done),
        'last_completed_step': last_done[-1] if last_done else None,
        'current_step': next((s for s in steps if not s['is_done']), None),
        'has_tasks': bool(steps),
    }, tool='get_order_timeline')


def get_worker_load():
    """بار کاری فعلی کارگران، از تسک‌های تخصیص‌یافته."""
    rows = (
        ProductionTask.objects
        .filter(assigned_worker__isnull=False, status__in=[
            queries.STATUS_PENDING, queries.STATUS_WAITING
        ])
        .values('assigned_worker_id', 'assigned_worker__username')
        .annotate(open_tasks=Count('id'))
        .order_by('-open_tasks')
    )
    workers = [{
        'user_id': row['assigned_worker_id'],
        'username': row['assigned_worker__username'],
        'open_tasks': row['open_tasks'],
    } for row in rows]
    return ok({
        'workers': workers,
        'workers_with_load': len(workers),
        'total_open_tasks': sum(w['open_tasks'] for w in workers),
        'statuses_counted': [queries.STATUS_PENDING, queries.STATUS_WAITING],
    }, tool='get_worker_load')


def get_working_day_info():
    """آیا امروز روز کاری است و چه تعطیلاتی ثبت شده — از منطق واقعی پروژه."""
    from product.models import Holiday

    today = jdatetime.date.today()
    gregorian = today.togregorian()
    upcoming = list(
        Holiday.objects
        .filter(date__gte=gregorian)
        .order_by('date')[:10]
    )
    return ok({
        'today_jalali': today.strftime('%Y/%m/%d'),
        'today_gregorian': gregorian.isoformat(),
        'is_working_day': is_working_day(today),
        'reason': 'جمعه‌ها و تعطیلات ثبت‌شده در جدول Holiday روز کاری نیستند.',
        'upcoming_holidays': [{
            'date': h.date.isoformat(),
            'description': h.description,
        } for h in upcoming],
    }, tool='get_working_day_info')


def register(registry):
    registry.register(Tool(
        name='get_order_timeline',
        description=(
            'ترتیب و وضعیت مراحل یک سفارش (ایستگاه‌ها به ترتیب step_order)، همراه با '
            'زمان‌بندی، کارگر تخصیص‌یافته و مرحلهٔ فعلی.'
        ),
        permission='production.view',
        read_only=True,
        category='planning',
        input_schema={
            'type': 'object',
            'properties': {
                'order_id': {'type': 'integer', 'description': 'شناسهٔ عددی سفارش.'},
            },
            'required': ['order_id'],
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'order_id': {'type': 'integer'},
                'steps': {'type': 'array'},
                'current_step': {'type': 'object'},
            },
        },
        handler=get_order_timeline,
    ))

    registry.register(Tool(
        name='get_worker_load',
        description=(
            'بار کاری کارگران بر اساس تعداد تسک‌های باز (آماده یا در انتظار) تخصیص‌یافته '
            'به هر کارگر.'
        ),
        permission='production.view',
        read_only=True,
        category='planning',
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
        output_schema={
            'type': 'object',
            'properties': {
                'workers': {'type': 'array'},
                'total_open_tasks': {'type': 'integer'},
            },
        },
        handler=get_worker_load,
    ))

    registry.register(Tool(
        name='get_working_day_info',
        description=(
            'آیا امروز روز کاری کارخانه است یا تعطیل، و تعطیلات پیش‌رو. از جدول تعطیلات '
            'و منطق روز کاری خود CraftFlow استفاده می‌کند.'
        ),
        permission='production.view',
        read_only=True,
        category='planning',
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
        output_schema={
            'type': 'object',
            'properties': {
                'is_working_day': {'type': 'boolean'},
                'today_jalali': {'type': 'string'},
            },
        },
        handler=get_working_day_info,
    ))