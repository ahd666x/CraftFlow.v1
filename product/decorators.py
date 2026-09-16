# product/decorators.py
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import redirect


def admin_or_manager_required(view_func=None, redirect_url='/orderlist/'):
    def check_user(user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name__in=['1', '2', '3']).exists()

    decorator = user_passes_test(check_user, login_url=redirect_url)
    
    if view_func:
        return decorator(view_func)
    return decorator


def staff_or_representative_required(view_func=None, redirect_url='/customer/orders/'):

    def check_user(user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        # چک کردن عضویت در گروه‌های مجاز
        return user.groups.filter(name__in=['1']).exists()

    decorator = user_passes_test(check_user, login_url=redirect_url)
    if view_func:
        return decorator(view_func)
    return decorator


def warehouse_required(view_func=None, redirect_url='/dashboard/'):
    def check_user(user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name='انبار').exists()

    decorator = user_passes_test(check_user, login_url=redirect_url)
    if view_func:
        return decorator(view_func)
    return decorator


def warehouse_or_manager_required(view_func=None, redirect_url='/dashboard/'):
    def check_user(user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return (
            user.groups.filter(name__in=['1', '2', '3']).exists()
            or user.groups.filter(name='انبار').exists()
        )

    decorator = user_passes_test(check_user, login_url=redirect_url)
    if view_func:
        return decorator(view_func)
    return decorator


def is_warehouse_user(user):
    """Helper callable (not a decorator) for branching inside a shared view like scan_packaging_unit."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name='انبار').exists()