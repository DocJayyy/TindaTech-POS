
from django.contrib.auth.decorators import user_passes_test


def is_owner(user):
    return (
        user.is_authenticated
        and (
            user.is_superuser
            or user.groups.filter(name="Owner").exists()
        )
    )


def is_cashier(user):
    return (
        user.is_authenticated
        and user.groups.filter(name="Cashier").exists()
    )


def can_use_pos(user):
    return is_owner(user) or is_cashier(user)


owner_required = user_passes_test(is_owner)
pos_access_required = user_passes_test(can_use_pos)
