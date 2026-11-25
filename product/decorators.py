# product/decorators.py
"""
منبع واحد سطوح دسترسی (r10) — ماتریس نقش‌های واقعی سیستم:

  superuser        : همه‌چیز — ایجاد/ویرایش/حذف سفارش، محصول، قطعه، آرشیو، مدیریت نقاشی، ادمین پنل
  گروه «1»         : کارگر داخلی — خواندن و اسکن (سفارش‌ها، گزارش‌ها، چاپ، اسکن، داشبورد،
                     صفحات نقاشی فقط برای دیدن)؛ هیچ ایجاد/ویرایشی ندارد.
  گروه «انبار»     : مثل قبل فقط بخش‌های انبار (inventory) + اسکن بسته‌بندی
  گروه «نماینده»   : فقط پنل نماینده (customer/*) + سه AJAXِ فرم سفارش نماینده
                     (ajax_load_products، ajax_load_product_colors و
                      ajax_load_product_dimensions — وابستهٔ customer/create_order.html)

نکته: گروه‌های «2» و «3» در این سیستم وجود ندارند و از همهٔ چک‌ها حذف شدند.
ویرایش/ثبت فقط از superuser است؛ گروه «1» روی ویوهای مخلوط (GET+POST/PUT/DELETE)
فقط متدهای امن (GET/HEAD/OPTIONS) را می‌بیند.
"""
from django.http import JsonResponse
from django.shortcuts import redirect
from functools import wraps

# نام گروه‌ها — تنها جای تعریف در کل پروژه
ROLE_WORKER = '1'                 # کارگر داخلی
ROLE_WAREHOUSE = 'انبار'
ROLE_REPRESENTATIVE = 'نماینده'

SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}


# ---------------------------------------------------------------------------
# Helpers (برای branching داخل ویوها، نه دکوراتور)
# ---------------------------------------------------------------------------
def user_group_names(user):
    if not getattr(user, 'is_authenticated', False):
        return set()
    return set(user.groups.values_list('name', flat=True))


def in_any_group(user, names):
    """superuser همیشه مجاز؛ در غیر این صورت عضویت در یکی از گروه‌های نام‌برده."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    return bool(user_group_names(user) & set(names))


def is_worker_user(user):
    """کارگر داخلی (گروه «1») — superuser عمداً شامل نمی‌شود؛ برای شاخه‌زدن داخل ویو."""
    return getattr(user, 'is_authenticated', False) and not user.is_superuser \
        and ROLE_WORKER in user_group_names(user)


def is_representative_user(user):
    """نماینده: عضو گروه «نماینده» و بدون هیچ گروه کارمندی."""
    if not getattr(user, 'is_authenticated', False) or getattr(user, 'is_superuser', False):
        return False
    names = user_group_names(user)
    return ROLE_REPRESENTATIVE in names and not names & {ROLE_WORKER, ROLE_WAREHOUSE}


def is_warehouse_user(user):
    """انبار — superuser هم مجاز؛ برای شاخه‌زدن داخل ویوهای مشترک."""
    return in_any_group(user, {ROLE_WAREHOUSE})


# ---------------------------------------------------------------------------
# هستهٔ مشترک دکوراتورها
# ---------------------------------------------------------------------------
def _denied_response(request, error):
    """AJAX → 403 JSON؛ HTML → None (فراخوان ریدایرکت می‌کند)."""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' \
            or request.content_type == 'application/json':
        return JsonResponse({'success': False, 'error': error}, status=403)
    return None


def _gate(check, redirect_url='/orderlist/', error='دسترسی غیرمجاز.'):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if check(request.user, request):
                return view_func(request, *args, **kwargs)
            resp = _denied_response(request, error)
            return resp if resp is not None else redirect(redirect_url)
        return _wrapped_view
    return decorator


def _optional(apply):
    """پشتیبانی از هر دو حالت @deco و @deco()."""
    def wrapper(view_func=None):
        if view_func is not None:
            return apply(view_func)
        return apply
    return wrapper


# ---------------------------------------------------------------------------
# دکوراتورهای عمومی
# ---------------------------------------------------------------------------
def group_required(*names, redirect_url='/orderlist/', error='دسترسی غیرمجاز.'):
    """superuser + اعضای گروه‌های نام‌برده — برای هر متد HTTP."""
    def apply(view_func):
        return _gate(
            lambda user, request: in_any_group(user, names),
            redirect_url, error,
        )(view_func)
    return _optional(apply)


def internal_required(view_func=None, redirect_url='/orderlist/',
                      error='دسترسی غیرمجاز. این بخش مخصوص همکاران داخلی است.'):
    """سطح خواندن/اسکن: superuser + گروه «1» — هر متد HTTP (اسکن‌ها POST دارند)."""
    def apply(vf):
        return _gate(
            lambda user, request: in_any_group(user, {ROLE_WORKER}),
            redirect_url, error,
        )(vf)
    return _optional(apply)(view_func) if view_func else _optional(apply)


def admin_required(view_func=None, redirect_url='/orderlist/',
                   error='دسترسی غیرمجاز. این عمل فقط برای ادمین مجاز است.'):
    """سطح نوشتن/مدیریت: فقط superuser."""
    def apply(vf):
        return _gate(
            lambda user, request: getattr(user, 'is_authenticated', False) and user.is_superuser,
            redirect_url, error,
        )(vf)
    return _optional(apply)(view_func) if view_func else _optional(apply)


def internal_read_admin_write(view_func=None, redirect_url='/orderlist/',
                              error='دسترسی غیرمجاز. ثبت تغییرات فقط برای ادمین مجاز است.'):
    """ویوهای مخلوط (مثل صفحات نقاشی GET+POST): متد امن برای superuser+گروه «1»؛
    متدهای نوشتن فقط superuser."""
    def check(user, request):
        if not getattr(user, 'is_authenticated', False):
            return False
        if user.is_superuser:
            return True
        if ROLE_WORKER in user_group_names(user):
            return request.method in SAFE_METHODS
        return False

    def apply(vf):
        return _gate(check, redirect_url, error)(vf)
    return _optional(apply)(view_func) if view_func else _optional(apply)


# ---------------------------------------------------------------------------
# پنل نماینده (بدون تغییر نسبت به r8)
# ---------------------------------------------------------------------------
def representative_panel_required(view_func=None, redirect_url='/shop/'):
    """دسترسی پنل نماینده (customer/*): ادمین، staff یا عضو گروه‌های «۱»/«نماینده».

    پنل نماینده قیمت‌های عمده (base_price) را نشان می‌دهد و ورودی ثبت سفارش
    عمده است؛ مطابق سیاست can_see_prices فروشگاه، کاربر عادیِ بدون مجوز قیمت
    نباید به آن برسد. برای کاربر لاگین‌شدهٔ بی‌مجوز، به فروشگاه (/shop/) برمی‌گردد.
    """
    def check_user(user):
        if not getattr(user, 'is_authenticated', False):
            return False
        if user.is_superuser or user.is_staff:
            return True
        return user_group_names(user) & {ROLE_WORKER, ROLE_REPRESENTATIVE}

    def decorator(vf):
        @wraps(vf)
        def _wrapped_view(request, *args, **kwargs):
            if check_user(request.user):
                return vf(request, *args, **kwargs)
            resp = _denied_response(
                request, 'دسترسی غیرمجاز. این بخش مخصوص نمایندگان است.')
            if resp is not None:
                return resp
            from django.contrib import messages
            messages.error(request, 'این بخش مخصوص نمایندگان است. برای ثبت سفارش از فروشگاه استفاده کنید.')
            return redirect(redirect_url)
        return _wrapped_view

    if view_func:
        return decorator(view_func)
    return decorator


# ---------------------------------------------------------------------------
# انبار — نام‌ها و رفتار قدیمی حفظ شده (inventory استفاده می‌کند)
# ---------------------------------------------------------------------------
def warehouse_required(view_func=None, redirect_url='/customer/orders/',
                       error='دسترسی غیرمجاز. کاربر باید عضو انبار باشد.'):
    """دسترسی انبار: superuser + گروه «انبار»."""
    def apply(vf):
        return _gate(lambda u, r: in_any_group(u, {ROLE_WAREHOUSE}),
                     redirect_url, error)(vf)
    return _optional(apply)(view_func) if view_func else _optional(apply)


def warehouse_or_manager_required(view_func=None, redirect_url='/customer/orders/',
                                  error='دسترسی غیرمجاز. کاربر باید انبار باشد.'):
    """انبار: superuser + گروه «انبار».

    (گروه‌های ۲ و ۳ قبلاً اینجا بودند اما در این سیستم وجود ندارند؛ رفتار
    مؤثر تغییری نکرده — همان superuser+انبار.)
    """
    def apply(vf):
        return _gate(lambda u, r: in_any_group(u, {ROLE_WAREHOUSE}),
                     redirect_url, error)(vf)
    return _optional(apply)(view_func) if view_func else _optional(apply)


def warehouse_receive_required(view_func=None, redirect_url='/customer/orders/',
                               error='دسترسی غیرمجاز. فقط کاربران انبار و ادمین مجاز هستند.'):
    """دریافت کالا: فقط انبار و ادمین."""
    def apply(vf):
        return _gate(lambda u, r: in_any_group(u, {ROLE_WAREHOUSE}),
                     redirect_url, error)(vf)
    return _optional(apply)(view_func) if view_func else _optional(apply)


def warehouse_deliver_required(view_func=None, redirect_url='/customer/orders/',
                               error='دسترسی غیرمجاز. فقط انبار و ادمین مجاز هستند.'):
    """تحویل مواد: انبار و ادمین (مدیرها حذف شدند — گروه ۲/۳ وجود ندارند)."""
    def apply(vf):
        return _gate(lambda u, r: in_any_group(u, {ROLE_WAREHOUSE}),
                     redirect_url, error)(vf)
    return _optional(apply)(view_func) if view_func else _optional(apply)


def day_close_required(view_func=None, redirect_url='/customer/orders/',
                       error='دسترسی غیرمجاز. فقط ادمین مجاز است.'):
    """تأیید پایان روز: فقط ادمین (گروه ۲/۳ وجود ندارند)."""
    return admin_required(view_func, redirect_url=redirect_url, error=error)
