# product/decorators.py
from django.shortcuts import redirect
from django.http import JsonResponse
from functools import wraps


def admin_or_manager_required(view_func=None, redirect_url='/orderlist/'):
    def check_user(user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name__in=['1', '2', '3']).exists()

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not check_user(request.user):
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.content_type == 'application/json':
                    return JsonResponse({'success': False, 'error': 'دسترسی غیرمجاز. کاربر باید ادمین یا مدیر باشد.'}, status=403)
                return redirect(redirect_url)
            return view_func(request, *args, **kwargs)
        return _wrapped_view

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

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not check_user(request.user):
                # برای درخواست‌های AJAX، JSON برگردان به جای ریدایرکت
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.content_type == 'application/json':
                    return JsonResponse({'success': False, 'error': 'دسترسی غیرمجاز. کاربر باید عضو گروه مجاز باشد.'}, status=403)
                return redirect(redirect_url)
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if view_func:
        return decorator(view_func)
    return decorator


def warehouse_required(view_func=None, redirect_url='/customer/orders/'):
    def check_user(user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name='انبار').exists()

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not check_user(request.user):
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.content_type == 'application/json':
                    return JsonResponse({'success': False, 'error': 'دسترسی غیرمجاز. کاربر باید عضو انبار باشد.'}, status=403)
                return redirect(redirect_url)
            return view_func(request, *args, **kwargs)
        return _wrapped_view

    if view_func:
        return decorator(view_func)
    return decorator


def warehouse_or_manager_required(view_func=None, redirect_url='/customer/orders/'):
    def check_user(user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return (
            user.groups.filter(name__in=['1', '2', '3']).exists()
            or user.groups.filter(name='انبار').exists()
        )

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not check_user(request.user):
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.content_type == 'application/json':
                    return JsonResponse({'success': False, 'error': 'دسترسی غیرمجاز. کاربر باید ادمین یا مدیر یا انبار باشد.'}, status=403)
                return redirect(redirect_url)
            return view_func(request, *args, **kwargs)
        return _wrapped_view

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