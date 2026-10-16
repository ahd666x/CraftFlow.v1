"""
دسترسی‌های سفارشی فروشگاه.
"""
REPRESENTATIVE_GROUP = 'نماینده'


def can_see_prices(user):
    """فقط نمایندگان، ادمین و کاربران staff قیمت می‌بینند."""
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return user.groups.filter(name=REPRESENTATIVE_GROUP).exists()
