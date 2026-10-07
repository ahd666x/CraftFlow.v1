"""
منطق مشترک محاسبه قیمت بر اساس تغییر ابعاد سفارشی محصول (طول/عرض/ارتفاع).
تنها منبع محاسبه قیمت ابعاد سفارشی در کل لایه‌ی فروشگاه — هم در سبد خرید
(نمایش لحظه‌ای) و هم هنگام ثبت نهایی سفارش باید از همین تابع استفاده شود تا
قیمت سبد و فاکتور نهایی هرگز مغایرت نداشته باشند.

فرمول: برای هر بعد جداگانه، اختلاف سانتی‌متری نسبت به پیش‌فرض ضربدر درصد
افزایش قیمت آن بعد. مجموع سه بعد در قیمت نهایی لحاظ می‌شود.
"""


def _dimension_diff(custom, default):
    """اختلاف یک بعد را نسبت به مقدار پیش‌فرض محصول برمی‌گرداند.
    اگر بعد پیش‌فرض یا بعد سفارشی مشخص نباشد، آن بعد اصلاً در قیمت تاثیر
    ندارد (اختلاف صفر در نظر گرفته می‌شود)."""
    if custom is None or default is None:
        return 0
    try:
        return int(custom) - int(default)
    except (TypeError, ValueError):
        return 0


def calculate_dimension_price(
    base_price,
    default_length,
    default_width,
    default_height,
    length_percent,
    width_percent,
    height_percent,
    length_editable,
    width_editable,
    height_editable,
    length=None,
    width=None,
    height=None,
):
    """قیمت نهایی را بر اساس ابعاد سفارشی محاسبه می‌کند.

    - base_price: قیمت پایه محصول
    - default_length/width/height: ابعاد پیش‌فرض محصول
    - length/width/height_percent: درصد افزایش قیمت به ازای هر سانتی‌متر هر بعد
    - length/width/height_editable: آیا مشتری می‌تواند آن بعد را تغییر دهد
    - length/width/height: ابعاد انتخابی مشتری

    اگر یک بعد editable=False باشد، آن بعد را کاملاً نادیده می‌گیر (حتی اگر مقدار
    متفاوتی پاس داده شده باشد) — یعنی diff آن بعد همیشه صفر در نظر گرفته شود.

    خروجی: عدد صحیح غیرمنفی
    """
    base_price_int = int(base_price or 0)

    diffs = 0
    if length_editable:
        diffs += _dimension_diff(length, default_length) * float(length_percent or 0)
    if width_editable:
        diffs += _dimension_diff(width, default_width) * float(width_percent or 0)
    if height_editable:
        diffs += _dimension_diff(height, default_height) * float(height_percent or 0)

    price_increase = base_price_int * (diffs / 100)
    final_price = base_price_int + price_increase

    if final_price < 0:
        return 0
    return int(round(final_price))
