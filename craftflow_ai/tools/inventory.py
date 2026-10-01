"""ابزارهای انبار — همه فقط-خواندنی و مبتنی بر منطق واقعی CraftFlow."""
from craftflow_ai.services import queries
from craftflow_ai.tools.registry import Tool, fail, ok


def get_inventory_status(limit=200):
    return ok(queries.inventory_status(limit=limit), tool='get_inventory_status')


def find_material_shortages(limit=50):
    return ok(queries.material_shortages(limit=limit), tool='find_material_shortages')


def get_material_requirements(order_id):
    if order_id is None:
        return fail('MISSING_ARGUMENT', 'شناسهٔ سفارش لازم است.', argument='order_id')
    data = queries.order_material_requirements(order_id)
    if data is None:
        return fail(
            'ORDER_NOT_FOUND',
            f'سفارشی با شناسهٔ {order_id} یافت نشد.',
            order_id=order_id,
        )
    return ok(data, tool='get_material_requirements')


def register(registry):
    registry.register(Tool(
        name='get_inventory_status',
        description=(
            'وضعیت موجودی مواد اولیه: موجودی واقعی هر ماده (بر پایهٔ گردش انبار)، '
            'حداقل موجودی هشدار، وضعیت کمبود، و درخواست‌های مواد باز.'
        ),
        permission='inventory.view',
        read_only=True,
        category='inventory',
        input_schema={
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer', 'description': 'حداکثر تعداد ماده (۱ تا ۱۰۰۰).'},
            },
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'total_materials': {'type': 'integer'},
                'low_stock_count': {'type': 'integer'},
                'out_of_stock_count': {'type': 'integer'},
                'materials': {'type': 'array'},
            },
        },
        handler=get_inventory_status,
    ))

    registry.register(Tool(
        name='find_material_shortages',
        description=(
            'مواد اولیهٔ کم‌موجود برای درخواست‌های باز تولید. محاسبه با همان منطق '
            'تحویل انبار CraftFlow انجام می‌شود: کسر از باقی‌ماندهٔ سالن، سپس گِرد کردن '
            'به بسته و مقایسه با موجودی انبار.'
        ),
        permission='inventory.view',
        read_only=True,
        category='inventory',
        input_schema={
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer', 'description': 'حداکثر تعداد ردیف (۱ تا ۲۰۰).'},
            },
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'shortage_count': {'type': 'integer'},
                'shortages': {'type': 'array'},
            },
        },
        handler=find_material_shortages,
    ))

    registry.register(Tool(
        name='get_material_requirements',
        description=(
            'مواد مورد نیاز یک سفارش: درخواست‌های ثبت‌شدهٔ انبار برای آن سفارش و '
            'نیاز محاسبه‌شده از فرمول ساخت (BOM) تسک‌های انجام‌نشده.'
        ),
        permission='inventory.view',
        read_only=True,
        category='inventory',
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
                'material_issues': {'type': 'array'},
                'requested_from_bom': {'type': 'array'},
            },
        },
        handler=get_material_requirements,
    ))