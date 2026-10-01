"""
ابزارهای شبیه‌سازی (فاز ۲) — فقط‌خواندنی و کاملاً خالص.

هیچ‌کدام از این ابزارها چیزی در دیتابیس نمی‌نویسد. شبیه‌سازی «اگر» را
روی یک snapshot دادهٔ واقعی محاسبه می‌کند و حالت پیش‌بینی‌شده را برمی‌گرداند.

آنچه **عمداً** شبیه‌سازی نمی‌شود و به‌جایش unavailable اعلام می‌شود:

* ظرفیت ایستگاه و دسترسی کارگر (در CraftFlow ذخیره نشده است)
* بهینه‌سازی ترتیب تولید (گراف وابستگی وجود ندارد)
* ورود سفارش به تولید (شمارش تسک ساختگی ممنوع است)

قاعدهٔ مهم اولویت: در CraftFlow اولویت فقط **ترتیب صف** را تغییر می‌دهد؛
روی ``step_order`` اثری ندارد و ``finish_time`` هم محاسبه نمی‌شود. بنابراین
خروجی هرگز زمانی را وعده نمی‌دهد.
"""
from decimal import Decimal, InvalidOperation

from craftflow_ai.simulation import scenarios
from craftflow_ai.tools.registry import Tool, fail, ok

ORDER_NOT_FOUND = 'ORDER_NOT_FOUND'
NOT_FOUND = 'NOT_FOUND'
INVALID_ARGUMENT = 'INVALID_ARGUMENT'


def _envelope(result, tool_name):
    payload = result.to_dict()
    return ok({
        'simulation': {
            'status': payload['status'],
            'confidence': payload['confidence'],
            'confidence_reasons': payload['confidence_reasons'],
            'limitations': payload['limitations'],
            'no_effect_reason': payload['no_effect_reason'],
        },
        'scenario_kind': payload['scenario_kind'],
        'projected_state': payload['projected_state'],
        'diff': payload['diff'],
        'evidence': payload['evidence'],
        'affected_changes': payload['affected_changes'],
        'write_performed': False,
    }, tool=tool_name)


# ----------------------------------------------------------------------
# simulate_material_availability
# ----------------------------------------------------------------------

def simulate_material_availability(raw_material_id, additional_quantity=0):
    """اگر X واحد به این ماده برسد، آیا تحویل درخواست‌های باز کافی می‌شود؟"""
    if raw_material_id is None:
        return fail(
            INVALID_ARGUMENT,
            'شناسهٔ مادهٔ اولیه لازم است.',
            argument='raw_material_id',
        )
    try:
        quantity = Decimal(str(additional_quantity if additional_quantity is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        return fail(
            INVALID_ARGUMENT,
            'مقدار اضافه باید عدد باشد.',
            argument='additional_quantity',
        )
    if quantity < 0:
        return fail(
            INVALID_ARGUMENT,
            'مقدار اضافه نمی‌تواند منفی باشد.',
            argument='additional_quantity',
        )

    result = scenarios.simulate_material_availability(raw_material_id, quantity)
    if result.no_effect_reason == 'raw_material_not_in_snapshot':
        return fail(
            NOT_FOUND,
            f'مادهٔ اولیه‌ای با شناسهٔ {raw_material_id} یافت نشد.',
            raw_material_id=raw_material_id,
        )
    return _envelope(result, 'simulate_material_availability')


# ----------------------------------------------------------------------
# simulate_order_priority
# ----------------------------------------------------------------------

def simulate_order_priority(order_id, new_priority):
    """اگر اولویت این سفارش عوض شود، جای آن در صف کجا می‌شود؟"""
    if order_id is None or new_priority is None:
        return fail(
            INVALID_ARGUMENT,
            'شناسهٔ سفارش و اولویت جدید هر دو لازم‌اند.',
            argument='order_id/new_priority',
        )
    try:
        priority_value = int(new_priority)
    except (TypeError, ValueError):
        return fail(
            INVALID_ARGUMENT,
            'اولویت باید عدد صحیح باشد.',
            argument='new_priority',
        )
    valid = [int(value) for value in scenarios.PRIORITY_CHOICES]
    if priority_value not in valid:
        return fail(
            INVALID_ARGUMENT,
            f'اولویت {priority_value} در CraftFlow تعریف نشده است.',
            argument='new_priority',
            allowed=valid,
        )

    result = scenarios.simulate_order_priority(order_id, priority_value)
    if result.no_effect_reason == 'order_not_in_open_queue':
        return fail(
            ORDER_NOT_FOUND,
            f'سفارش {order_id} در صف سفارش‌های باز نیست.',
            order_id=order_id,
        )
    return _envelope(result, 'simulate_order_priority')


def register(registry):
    registry.register(Tool(
        name='simulate_material_availability',
        description=(
            'شبیه‌سازی خالص: اگر مقدار مشخصی به موجودی یک مادهٔ اولیه اضافه شود، '
            'آیا درخواست‌های باز آن با همان ریاضیات واقعی تحویل انبار پوشش داده '
            'می‌شوند؟ موجودی فعلی، موجودی پیش‌بینی‌شده، نیاز فیزیکی، کمبود و اثر '
            'باقی‌ماندهٔ سالن را برمی‌گرداند. هیچ چیزی نوشته نمی‌شود.'
        ),
        permission='inventory.view',
        read_only=True,
        category='simulation',
        input_schema={
            'type': 'object',
            'properties': {
                'raw_material_id': {
                    'type': 'integer',
                    'description': 'شناسهٔ مادهٔ اولیه در انبار.',
                },
                'additional_quantity': {
                    'type': 'number',
                    'description': 'مقدار فرضی ورود کالا به انبار.',
                },
            },
            'required': ['raw_material_id'],
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'simulation': {'type': 'object'},
                'projected_state': {'type': 'object'},
                'diff': {'type': 'array'},
                'write_performed': {'type': 'boolean'},
            },
        },
        handler=simulate_material_availability,
    ))

    registry.register(Tool(
        name='simulate_order_priority',
        description=(
            'شبیه‌سازی خالص: اگر اولویت یک سفارش تغییر کند، جای آن در صف '
            'سفارش‌های باز کجا می‌شود و کدام سفارش‌ها جابه‌جا می‌شوند. توجه: در '
            'CraftFlow اولویت فقط ترتیب صف را عوض می‌کند؛ روی ترتیب مراحل تولید '
            'اثری ندارد و زمان اتمام محاسبه نمی‌شود.'
        ),
        permission='orders.view',
        read_only=True,
        category='simulation',
        input_schema={
            'type': 'object',
            'properties': {
                'order_id': {'type': 'integer', 'description': 'شناسهٔ سفارش.'},
                'new_priority': {
                    'type': 'integer',
                    'description': 'اولویت فرضی جدید؛ یکی از ۱ (بسیار بالا) تا ۴ (پایین).',
                },
            },
            'required': ['order_id', 'new_priority'],
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'simulation': {'type': 'object'},
                'projected_state': {'type': 'object'},
                'diff': {'type': 'array'},
                'write_performed': {'type': 'boolean'},
            },
        },
        handler=simulate_order_priority,
    ))


__all__ = ['register', 'simulate_material_availability', 'simulate_order_priority']