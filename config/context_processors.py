from portfolio.user_management import can_user_manage_financial_data, can_user_manage_users, is_user_management_recovery_mode


def user_management_context(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {
            "can_manage_finances": False,
            "can_manage_users": False,
            "user_management_recovery_mode": False,
        }

    recovery_mode = is_user_management_recovery_mode()
    return {
        "can_manage_finances": can_user_manage_financial_data(user),
        "can_manage_users": can_user_manage_users(user, recovery_mode=recovery_mode),
        "user_management_recovery_mode": recovery_mode,
    }
