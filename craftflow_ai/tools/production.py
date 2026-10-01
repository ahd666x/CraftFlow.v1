"""ابزارهای وضعیت تولید — همه فقط-خواندنی."""
from craftflow_ai.services import queries
from craftflow_ai.tools.registry import Tool, ok


def get_production_status():
    return ok(queries.production_status(), tool='get_production_status')


def get_pending_tasks(stage=None, order_id=None, limit=25):
    data = queries.pending_tasks(stage=stage, order_id=order_id, limit=limit)
    if data.get('error'):
        from craftflow_ai.tools.registry import fail
        return fail(
            'UNKNOWN_STAGE',
            f'ایستگاه «{stage}» در CraftFlow وجود ندارد.',
            **data,
        )
    return ok(data, tool='get_pending_tasks')


def find_production_bottlenecks(limit=5):
    return ok(queries.bottlenecks(limit=limit), tool='find_production_bottlenecks')


def register(registry):
    registry.register(Tool(
        name='get_production_status',
        description=(
            'وضعیت کلی تولید در همین لحظه: تعداد تسک‌های آماده/در انتظار/در حال اجرا/'
            'مسدود/تکمیل‌شده به تفکیک ایستگاه، شلوغ‌ترین ایستگاه، وضعیت سفارش‌ها، '
            'بسته‌بندی و ارسال امروز و تعداد خرابی‌های باز.'
        ),
        permission='production.view',
        read_only=True,
        category='production',
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
        output_schema={
            'type': 'object',
            'properties': {
                'tasks': {'type': 'object'},
                'stations': {'type': 'array'},
                'busiest_stage': {'type': 'object'},
            },
        },
        handler=get_production_status,
    ))

    registry.register(Tool(
        name='get_pending_tasks',
        description=(
            'تسک‌های آمادهٔ انجام (وضعیت pending) به همراه سفارش، قطعه، ایستگاه، '
            'تعداد، کارگر تخصیص‌یافته و زمان‌بندی. با فیلتر اختیاری ایستگاه یا سفارش.'
        ),
        permission='production.view',
        read_only=True,
        category='production',
        input_schema={
            'type': 'object',
            'properties': {
                'stage': {
                    'type': 'string',
                    'description': (
                        'کد ایستگاه در CraftFlow؛ دقیقاً یکی از: cut، cnc، dr، '
                        'pvc، prs، mon، vacum، paint، assembly2، packaging، shipping.'
                    ),
                },
                'order_id': {'type': 'integer', 'description': 'فقط تسک‌های این سفارش.'},
                'limit': {
                    'type': 'integer',
                    'description': 'حداکثر تعداد ردیف بازگشتی (۱ تا ۲۰۰).',
                },
            },
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {'total': {'type': 'integer'}, 'tasks': {'type': 'array'}},
        },
        handler=get_pending_tasks,
    ))

    registry.register(Tool(
        name='find_production_bottlenecks',
        description=(
            'ایستگاه‌های دارای گلوگاه، بر اساس دادهٔ واقعی: تعداد تسک آماده، در انتظار، '
            'در حال اجرا و عقب‌افتاده به‌علاوهٔ قدیمی‌ترین تسک آمادهٔ انجام و علت.'
        ),
        permission='production.view',
        read_only=True,
        category='production',
        input_schema={
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer', 'description': 'تعداد ایستگاه‌های برتر (۱ تا ۲۰).'},
            },
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'bottlenecks': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'stage': {'type': 'string'},
                            'pending_tasks': {'type': 'integer'},
                            'blocked_tasks': {'type': 'integer'},
                            'oldest_pending': {'type': 'object'},
                            'reason': {'type': 'string'},
                        },
                    },
                },
            },
        },
        handler=find_production_bottlenecks,
    ))