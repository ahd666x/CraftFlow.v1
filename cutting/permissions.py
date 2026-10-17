OPERATOR_GROUPS = ('1', '2', '3', 'انبار')
INVENTORY_GROUPS = ('2', '3', 'انبار')
MANAGER_GROUPS = ('2', '3')


def _in_groups(user, names):
    return user.groups.filter(name__in=names).exists()


def can_operate(user):
    return user.is_authenticated and (
        user.is_superuser or user.is_staff or _in_groups(user, OPERATOR_GROUPS)
    )


def can_inventory(user):
    return user.is_authenticated and (
        user.is_superuser or _in_groups(user, INVENTORY_GROUPS)
    )


def can_manage(user):
    return user.is_authenticated and (
        user.is_superuser or _in_groups(user, MANAGER_GROUPS)
    )
