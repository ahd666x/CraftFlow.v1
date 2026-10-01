"""
ابزارهای تحلیل قطعی (فاز ۲).

این ماژول تنها «پل» بین لایهٔ تحلیل و رجیستری ابزار است. هیچ منطق کسب‌وکاری
اینجا نیست: تحلیل در ``craftflow_ai.analysis`` انجام می‌شود و اینجا فقط
نتیجهٔ ساختاریافتهٔ آن به شکل خروجی استاندارد Tool بسته‌بندی می‌شود.

قرارداد خروجی (بخش ۲۷ پروتکل):

    {
      "success": true,
      "data": {"analysis": {"status", "confidence", "confidence_reasons",
                            "findings", "limitations", ...}},
      "metadata": {"generated_at", "source", "tool"}
    }

دادهٔ ناکافی هرگز به صفر تبدیل نمی‌شود؛ ``status`` می‌شود ``insufficient_data``
یا ``unknown`` و ``findings`` خالی می‌ماند.
"""
from craftflow_ai.analysis import inventory as analysis_inventory
from craftflow_ai.analysis import order as analysis_order
from craftflow_ai.analysis import production as analysis_production
from craftflow_ai.tools.registry import Tool, fail, ok

ORDER_NOT_FOUND = 'ORDER_NOT_FOUND'
NOT_FOUND = 'NOT_FOUND'
INVALID_STAGE = 'INVALID_STAGE'


# ----------------------------------------------------------------------
# analyze_order_health
# ----------------------------------------------------------------------

def analyze_order_health(order_id):
    """سلامت شش‌دامنه‌ای سفارش با وضعیت و اطمینان قطعی برای هر دامنه."""
    report = analysis_order.order_health(order_id)
    if report is None:
        return fail(
            ORDER_NOT_FOUND,
            f'سفارشی با شناسهٔ {order_id} یافت نشد.',
            order_id=order_id,
        )
    return ok({'analysis': report}, tool='analyze_order_health')


# ----------------------------------------------------------------------
# analyze_order_delay
# ----------------------------------------------------------------------

def analyze_order_delay(order_id, days=None):
    """علت قطعی تأخیر با تقدم ثابت و شدت از رجیستری قطعی."""
    report = analysis_order.order_delay(order_id, days=days)
    if report is None:
        return fail(
            ORDER_NOT_FOUND,
            f'سفارشی با شناسهٔ {order_id} یافت نشد.',
            order_id=order_id,
        )
    if not report['is_delayed']:
        return ok({
            'analysis': {
                'status': 'not_delayed',
                'confidence': report['confidence'],
                'confidence_reasons': report['confidence_reasons'],
                'findings': [],
                'limitations': report['limitations'],
                'limitations_detail': report['limitations_detail'],
            },
            'delay': report,
        }, tool='analyze_order_delay')

    return ok({
        'analysis': {
            'status': 'delayed',
            'confidence': report['confidence'],
            'confidence_reasons': report['confidence_reasons'],
            'findings': report['findings'],
            'limitations': report['limitations'],
            'limitations_detail': report['limitations_detail'],
        },
        'severity': report['severity'],
        'cause_code': report['cause_code'],
        'delay': report,
    }, tool='analyze_order_delay')


# ----------------------------------------------------------------------
# analyze_station_bottleneck
# ----------------------------------------------------------------------

def analyze_station_bottleneck(stage):
    """گلوگاه ایستگاه با تعداد سفارش‌های درگیر و سطل ایستگاه ناشناس."""
    valid = [code for code, _label in analysis_production.STATION_LABELS.items()]
    if str(stage or '').strip() not in valid:
        return fail(
            INVALID_STAGE,
            f'ایستگاه «{stage}» در CraftFlow وجود ندارد.',
            available_stages=[{'stage': c} for c in valid],
        )

    report = analysis_production.station_bottleneck(stage)
    return ok({
        'analysis': {
            'status': report['status'],
            'confidence': report['confidence'],
            'confidence_reasons': report['confidence_reasons'],
            'findings': report['findings'],
            'limitations': report['limitations'],
            'limitations_detail': report['limitations_detail'],
        },
        'station': report,
        'unknown_station': report['unknown_station'],
    }, tool='analyze_station_bottleneck')


# ----------------------------------------------------------------------
# analyze_material_impact
# ----------------------------------------------------------------------

def analyze_material_impact(raw_material_id=None, order_id=None, limit=25):
    """
    تأثیر مواد روی سفارش‌های باز، از سمت انبار.

    نیاز مواد از BOM به‌دلیل ``Material.raw_material = NULL`` گزارش نمی‌شود و
    ``insufficient_data`` برمی‌گردد.
    """
    if raw_material_id is None and order_id is None:
        return ok({
            'analysis': {
                'status': 'insufficient_data',
                'confidence': 'low',
                'confidence_reasons': ['حداقل یکی از raw_material_id یا order_id لازم است.'],
                'findings': [],
                'limitations': ['missing_material_mapping'],
                'limitations_detail': [],
            },
            'report_status': 'insufficient_data',
        }, tool='analyze_material_impact')

    report = analysis_inventory.material_impact(
        raw_material_id=raw_material_id, order_id=order_id, limit=limit,
    )
    if report is None:
        return fail(
            NOT_FOUND,
            f'مادهٔ اولیه یا سفارشی با این شناسه یافت نشد.',
            raw_material_id=raw_material_id,
            order_id=order_id,
        )

    limitations = list(report.get('limitations') or [])
    findings = report.get('findings') or []

    # وضعیت از دامنهٔ واقعاً پرسیده‌شده می‌آید: دامنهٔ انبار دادهٔ کامل دارد،
    # اما دامنهٔ BOM یک سفارش به‌دلیل Material.raw_material خالی نیست.
    if report.get('scope') == 'warehouse':
        status = report.get('report_status', 'insufficient_data')
    else:
        status = report.get('status') or report.get('report_status') or 'insufficient_data'

    return ok({
        'analysis': {
            'status': status,
            'confidence': report['confidence'],
            'confidence_reasons': report.get('confidence_reasons') or [],
            'findings': findings,
            'limitations': sorted(set(limitations)),
            'limitations_detail': report.get('limitations_detail') or [],
        },
        'materials': report,
    }, tool='analyze_material_impact')


# ----------------------------------------------------------------------
# ثبت در رجیستری
# ----------------------------------------------------------------------

def register(registry):
    registry.register(Tool(
        name='analyze_order_health',
        description=(
            'تحلیل قطعی سلامت یک سفارش در شش دامنه: تولید، مواد، نقاشی، کیفیت، '
            'بسته‌بندی و ارسال. هر دامنه وضعیت و سطح اطمینان خودش را دارد و '
            'محدودیت‌های داده صریح گزارش می‌شوند. اعداد را خودش حساب می‌کند.'
        ),
        permission='orders.view',
        read_only=True,
        category='analysis',
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
                'analysis': {'type': 'object'},
                'status': {'type': 'string'},
                'confidence': {'type': 'string'},
            },
        },
        handler=analyze_order_health,
    ))

    registry.register(Tool(
        name='analyze_order_delay',
        description=(
            'علت قطعی تأخیر یک سفارش با ترتیب تقدم ثابت (مواد، کیفیت، مرحلهٔ قبل، '
            'صف ایستگاه، زمان‌بندی، سن سفارش) و شدت از رجیستری ثابت. اگر '
            'due_date خالی باشد، تأخیر فقط با قاعدهٔ CraftFlow سنجیده می‌شود و '
            'اطمینان low است.'
        ),
        permission='orders.view',
        read_only=True,
        category='analysis',
        input_schema={
            'type': 'object',
            'properties': {
                'order_id': {'type': 'integer', 'description': 'شناسهٔ عددی سفارش.'},
                'days': {
                    'type': 'integer',
                    'description': (
                        'آستانهٔ قاعدهٔ CraftFlow برای «عقب‌افتاده». پیش‌فرض همان '
                        '۳ روز خود سامانه است.'
                    ),
                },
            },
            'required': ['order_id'],
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'analysis': {'type': 'object'},
                'severity': {'type': 'string'},
                'cause_code': {'type': 'string'},
                'delay': {'type': 'object'},
            },
        },
        handler=analyze_order_delay,
    ))

    registry.register(Tool(
        name='analyze_station_bottleneck',
        description=(
            'تحلیل قطعی گلوگاه یک ایستگاه: تعداد pending/waiting/done، تسک‌های '
            'در حال اجرا و مسدود (مشتق‌شده)، تعداد و فهرست سفارش‌های درگیر، '
            'کارگران تخصیص‌یافته، و سطل صریح ایستگاه‌های ناشناس. ظرفیت عددی '
            'ایستگاه در CraftFlow ذخیره نشده و گزارش نمی‌شود.'
        ),
        permission='production.view',
        read_only=True,
        category='analysis',
        input_schema={
            'type': 'object',
            'properties': {
                'stage': {
                    'type': 'string',
                    'description': (
                        'کد ایستگاه؛ یکی از cut، cnc، dr، pvc، prs، mon، vacum، '
                        'paint، assembly2، packaging، shipping.'
                    ),
                },
            },
            'required': ['stage'],
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'analysis': {'type': 'object'},
                'station': {'type': 'object'},
                'unknown_station': {'type': 'object'},
            },
        },
        handler=analyze_station_bottleneck,
    ))

    registry.register(Tool(
        name='analyze_material_impact',
        description=(
            'تأثیر یک مادهٔ اولیه یا یک سفارش روی موجودی و سفارش‌های باز: موجودی '
            'واقعی، وضعیت کمبود، درخواست‌های باز، و تعداد سفارش‌های درگیر. نیاز '
            'مواد از BOM قابل محاسبه نیست چون Material.raw_material خالی است و '
            'در این حالت insufficient_data برمی‌گردد.'
        ),
        permission='inventory.view',
        read_only=True,
        category='analysis',
        input_schema={
            'type': 'object',
            'properties': {
                'raw_material_id': {
                    'type': 'integer',
                    'description': 'فقط یک مادهٔ اولیهٔ مشخص.',
                },
                'order_id': {
                    'type': 'integer',
                    'description': 'نمای مواد یک سفارش از سمت درخواست‌های انبار.',
                },
                'limit': {
                    'type': 'integer',
                    'description': 'حداکثر تعداد ماده در گزارش انبار (۱ تا ۱۰۰).',
                },
            },
            'additionalProperties': False,
        },
        output_schema={
            'type': 'object',
            'properties': {
                'analysis': {'type': 'object'},
                'materials': {'type': 'object'},
            },
        },
        handler=analyze_material_impact,
    ))


__all__ = ['analyze_material_impact', 'analyze_order_delay', 'analyze_order_health',
           'analyze_station_bottleneck', 'register']