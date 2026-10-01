"""ابزارهای گزارش و کیفیت — همه فقط-خواندنی."""
from craftflow_ai.services import queries
from craftflow_ai.tools.registry import Tool, ok


def generate_production_report():
    return ok(queries.production_report(), tool='generate_production_report')


def get_quality_status(limit=50):
    return ok(queries.quality_status(limit=limit), tool='get_quality_status')


def register(registry):
    registry.register(Tool(
        name='generate_production_report',
        description=(
            'گزارش یکپارچهٔ تولید: وضعیت کلی، گلوگاه‌ها، سفارش‌های عقب‌افتاده، '
            'کمبود مواد و سفارش‌های باز — همه از داده‌های واقعی دیتابیس.'
        ),
        permission='reports.view',
        read_only=True,
        category='reports',
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
        output_schema={
            'type': 'object',
            'properties': {
                'today': {'type': 'string'},
                'production': {'type': 'object'},
                'bottlenecks': {'type': 'array'},
                'delayed_orders': {'type': 'array'},
                'material_shortages': {'type': 'array'},
            },
        },
        handler=generate_production_report,
    ))


def register_quality(registry):
    registry.register(Tool(
        name='get_quality_status',
        description=(
            'وضعیت خرابی‌های تولید: تعداد خرابی باز به تفکیک وضعیت و آخرین خرابی‌های '
            'ثبت‌شده به همراه سفارش، تعداد و روند نقاشی مربوطه.'
        ),
        permission='quality.view',
        read_only=True,
        category='quality',
        input_schema={
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer', 'description': 'حداکثر تعداد خرابی اخیر (۱ تا ۲۰۰).'},
            },
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'total_open': {'type': 'integer'},
                'by_status': {'type': 'object'},
                'recent': {'type': 'array'},
            },
        },
        handler=get_quality_status,
    ))