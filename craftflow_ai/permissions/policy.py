"""
سیاست دسترسی لایهٔ AI.

CraftFlow دسترسی را با «نام گروه» پیاده می‌کند، نه با Django Permission
(به ``product/decorators.py`` نگاه کنید: گروه‌های ``'1'`` کارمند،
``'2'``/``'3'`` مدیر و ``'انبار'`` انباردار). برای اینکه رفتار AI با رفتار
بقیهٔ سیستم یکی بماند، همان گروه‌ها اینجا استفاده می‌شوند و نام‌ها از
``settings`` قابل تغییر است.

هر Tool پیش از اجرا ``authorize(user)`` را صدا می‌زند. خطای دسترسی هرگز
به‌صورت پیام دوستانه به LLM برنمی‌گردد؛ فقط به‌صورت کد خطای ساختاریافته.
"""
from dataclasses import dataclass, field

from django.conf import settings

from .errors import ToolPermissionError

# نام گروه‌های پیش‌فرض CraftFlow
DEFAULT_MANAGER_GROUPS = ('1', '2', '3')
DEFAULT_STAFF_GROUPS = ('1',)
DEFAULT_WAREHOUSE_GROUPS = ('انبار',)


@dataclass(frozen=True)
class Policy:
    """یک قاعدهٔ دسترسی: چه گروه‌هایی اجازهٔ اجرای یک Tool را دارند."""
    key: str
    groups: tuple = field(default_factory=tuple)
    description: str = ''
    allow_superuser: bool = True


def _groups(setting_name, default):
    value = getattr(settings, setting_name, None)
    if not value:
        return tuple(default)
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(',') if v.strip())
    return tuple(value)


def manager_groups():
    return _groups('CRAFTFLOW_AI_MANAGER_GROUPS', DEFAULT_MANAGER_GROUPS)


def staff_groups():
    return _groups('CRAFTFLOW_AI_STAFF_GROUPS', DEFAULT_STAFF_GROUPS)


def warehouse_groups():
    return _groups('CRAFTFLOW_AI_WAREHOUSE_GROUPS', DEFAULT_WAREHOUSE_GROUPS)


def _build_policies():
    manager = manager_groups()
    staff = staff_groups()
    warehouse = warehouse_groups()
    return {
        'orders.view': Policy(
            'orders.view', manager + staff,
            'مشاهدهٔ سفارش‌ها',
        ),
        'production.view': Policy(
            'production.view', manager + staff,
            'مشاهدهٔ وضعیت تولید و تسک‌ها',
        ),
        'inventory.view': Policy(
            'inventory.view', manager + warehouse + staff,
            'مشاهدهٔ موجودی و درخواست‌های مواد',
        ),
        'reports.view': Policy(
            'reports.view', manager + staff,
            'مشاهدهٔ گزارش‌ها',
        ),
        'quality.view': Policy(
            'quality.view', manager + staff,
            'مشاهدهٔ خرابی‌ها',
        ),
    }


POLICIES = _build_policies()


def known_permissions():
    return sorted(POLICIES.keys())


def user_group_names(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return set()
    if user.is_superuser:
        return {'__superuser__'}
    return set(user.groups.values_list('name', flat=True))


def has_permission(user, permission_key):
    policy = POLICIES.get(permission_key)
    if policy is None:
        # یک permission تعریف‌نشده یعنی پیکربندی اشتباه است؛ محافظه‌کارانه بسته است.
        return False
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if policy.allow_superuser and user.is_superuser:
        return True
    groups = user_group_names(user)
    return bool(groups & set(policy.groups))


def authorize(user, permission_key):
    """بررسی دسترسی؛ در صورت نداشتن دسترسی ``ToolPermissionError`` پرتاب می‌شود."""
    if has_permission(user, permission_key):
        return True
    policy = POLICIES.get(permission_key)
    raise ToolPermissionError(
        f'کاربر اجازهٔ «{permission_key}» را ندارد.',
        code='PERMISSION_DENIED',
        detail={
            'permission': permission_key,
            'required_groups': list(policy.groups) if policy else [],
            'user_groups': sorted(user_group_names(user)),
        },
    )


def permissions_for_user(user):
    """فهرست permissionهای مؤثر کاربر — برای نمایش شفاف در UI."""
    return {key: has_permission(user, key) for key in known_permissions()}