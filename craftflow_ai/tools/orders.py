"""ابزارهای مربوط به سفارش‌ها — همه فقط-خواندنی."""
from craftflow_ai.services import queries
from craftflow_ai.tools.registry import Tool, fail, ok

LIMIT_SCHEMA = {
    'type': 'integer',
    'description': 'حداکثر تعداد ردیف بازگشتی (۱ تا ۲۰۰).',
}


def get_open_orders(limit=25):
    return ok(queries.open_orders(limit=limit), tool='get_open_orders')


def get_order_details(order_id):
    if order_id is None:
        return fail('MISSING_ARGUMENT', 'شناسهٔ سفارش لازم است.', argument='order_id')
    data = queries.order_details(order_id)
    if data is None:
        return fail(
            'ORDER_NOT_FOUND',
            f'سفارشی با شناسهٔ {order_id} یافت نشد.',
            order_id=order_id,
        )
    return ok(data, tool='get_order_details')


def find_delayed_orders(limit=25, days=None):
    data = queries.delayed_orders(limit=limit, days=days)
    return ok(data, tool='find_delayed_orders')


def register(registry):
    registry.register(Tool(
        name='get_open_orders',
        description=(
            'لیست سفارش‌های باز (پیش‌نویس، برنامه‌ریزی‌شده، در حال تولید) به همراه '
            'مشتری، اولویت، تعداد تسک‌ها و درصد پیشرفت واقعی هر سفارش.'
        ),
        permission='orders.view',
        read_only=True,
        category='orders',
        input_schema={
            'type': 'object',
            'properties': {'limit': LIMIT_SCHEMA},
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'total_open_orders': {'type': 'integer'},
                'orders': {'type': 'array'},
            },
        },
        handler=get_open_orders,
    ))

    registry.register(Tool(
        name='get_order_details',
        description=(
            'اطلاعات عملیاتی کامل یک سفارش: آیتم‌ها، رنگ‌ها، پیشرفت هر ایستگاه تولید، '
            'مرحلهٔ فعلی، وضعیت بسته‌بندی/ارسال، خرابی‌ها و درخواست‌های مواد باز.'
        ),
        permission='orders.view',
        read_only=True,
        category='orders',
        input_schema={
            'type': 'object',
            'properties': {
                'order_id': {
                    'type': 'integer',
                    'description': 'شناسهٔ عددی سفارش در CraftFlow.',
                },
            },
            'required': ['order_id'],
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'order_id': {'type': 'integer'},
                'status': {'type': 'string'},
                'stages': {'type': 'array'},
            },
        },
        handler=get_order_details,
    ))

    registry.register(Tool(
        name='find_delayed_orders',
        description=(
            'سفارش‌های عقب‌افتاده: سفارش‌های ناتمامی که بیش از N روز از ثبتشان گذشته '
            '(همان تعریف گزارش «سفارش‌های عقب‌افتاده» در CraftFlow).'
        ),
        permission='orders.view',
        read_only=True,
        category='orders',
        input_schema={
            'type': 'object',
            'properties': {
                'limit': LIMIT_SCHEMA,
                'days': {
                    'type': 'integer',
                    'description': 'سقف روزها؛ پیش‌فرض همان ۳ روز گزارش CraftFlow است.',
                },
            },
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'total_delayed': {'type': 'integer'},
                'delayed_orders': {'type': 'array'},
            },
        },
        handler=find_delayed_orders,
    ))