from django.contrib.auth import get_user_model
from django.db.models import Q


def get_active_admin_count() -> int:
    User = get_user_model()
    return User.objects.filter(is_active=True).filter(Q(is_staff=True) | Q(is_superuser=True)).count()


def is_user_management_recovery_mode() -> bool:
    return get_active_admin_count() == 0


def can_user_manage_users(user, recovery_mode: bool | None = None) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    if recovery_mode is None:
        recovery_mode = is_user_management_recovery_mode()
    return recovery_mode


def set_user_admin_flags(user, is_admin: bool) -> None:
    user.is_staff = is_admin
    user.is_superuser = is_admin
