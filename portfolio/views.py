from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from .forms import ACCESS_LEVEL_CHOICES, ManagedUserCreateForm
from .services import build_portfolio_dashboard, capture_portfolio_snapshot
from .user_management import can_user_manage_users, get_active_admin_count, is_user_management_recovery_mode, set_user_admin_flags


class PortfolioDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "portfolio/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(build_portfolio_dashboard())
        return context

    def post(self, request, *args, **kwargs):
        capture_portfolio_snapshot()
        messages.success(request, "Se ha guardado la foto de cartera de hoy.")
        return redirect("portfolio:dashboard")


class UserManagementView(LoginRequiredMixin, TemplateView):
    template_name = "portfolio/user_management.html"

    def dispatch(self, request, *args, **kwargs):
        if not can_user_manage_users(request.user):
            messages.error(
                request,
                "Solo un administrador puede gestionar usuarios. Si no queda ningun admin activo, entra con cualquier usuario y se abrira el modo recuperacion.",
            )
            return redirect("portfolio:dashboard")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        User = get_user_model()
        managed_users = list(User.objects.order_by("-is_active", "-is_staff", "username"))
        active_admin_count = get_active_admin_count()
        recovery_mode = is_user_management_recovery_mode()

        for managed_user in managed_users:
            managed_user.access_level = "admin" if (managed_user.is_staff or managed_user.is_superuser) else "user"
            managed_user.role_label = "Administrador" if managed_user.access_level == "admin" else "Usuario"
            managed_user.is_current_user = managed_user.pk == self.request.user.pk
            managed_user.is_last_active_admin = (
                managed_user.is_active
                and managed_user.access_level == "admin"
                and active_admin_count == 1
            )

        context["page_title"] = "Usuarios"
        context["create_form"] = kwargs.get("create_form", ManagedUserCreateForm())
        context["managed_users"] = managed_users
        context["access_level_choices"] = ACCESS_LEVEL_CHOICES
        context["users_summary"] = {
            "total": len(managed_users),
            "active": sum(1 for user in managed_users if user.is_active),
            "admins": sum(1 for user in managed_users if user.access_level == "admin"),
        }
        context["recovery_mode"] = recovery_mode
        context["can_promote_self_to_admin"] = recovery_mode and not (self.request.user.is_staff or self.request.user.is_superuser)
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "").strip()
        if action == "create_user":
            return self._create_user(request)
        if action == "update_user_role":
            return self._update_user_role(request)
        if action == "toggle_user_active":
            return self._toggle_user_active(request)
        if action == "promote_self_to_admin":
            return self._promote_self_to_admin(request)
        messages.error(request, "La accion de usuarios no es valida.")
        return redirect("portfolio:user_management")

    def _get_managed_user(self, request):
        User = get_user_model()
        user_id = request.POST.get("user_id", "").strip()
        return User.objects.filter(pk=user_id).first()

    def _parse_access_level(self, raw_value: str) -> str | None:
        access_level = str(raw_value or "").strip()
        valid_values = {choice[0] for choice in ACCESS_LEVEL_CHOICES}
        return access_level if access_level in valid_values else None

    def _create_user(self, request):
        form = ManagedUserCreateForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(create_form=form)
            return self.render_to_response(context, status=400)

        User = get_user_model()
        user = User(username=form.cleaned_data["username"])
        user.is_active = True
        set_user_admin_flags(user, form.cleaned_data["access_level"] == "admin")
        user.set_password(form.cleaned_data["password1"])
        user.save()
        messages.success(request, f"El usuario {user.username} se ha creado correctamente.")
        return redirect("portfolio:user_management")

    def _update_user_role(self, request):
        managed_user = self._get_managed_user(request)
        if not managed_user:
            messages.error(request, "No se ha encontrado el usuario indicado.")
            return redirect("portfolio:user_management")

        access_level = self._parse_access_level(request.POST.get("access_level"))
        if not access_level:
            messages.error(request, "El nivel de acceso no es valido.")
            return redirect("portfolio:user_management")

        wants_admin = access_level == "admin"
        is_current_admin = managed_user.is_staff or managed_user.is_superuser
        if is_current_admin and not wants_admin and managed_user.is_active and get_active_admin_count() <= 1:
            messages.error(request, "No puedes quitar el ultimo administrador activo.")
            return redirect("portfolio:user_management")

        set_user_admin_flags(managed_user, wants_admin)
        managed_user.save(update_fields=["is_staff", "is_superuser"])
        role_label = "Administrador" if wants_admin else "Usuario"
        messages.success(request, f"{managed_user.username} ahora tiene el rol {role_label}.")
        return redirect("portfolio:user_management")

    def _toggle_user_active(self, request):
        managed_user = self._get_managed_user(request)
        if not managed_user:
            messages.error(request, "No se ha encontrado el usuario indicado.")
            return redirect("portfolio:user_management")

        if managed_user.is_active and (managed_user.is_staff or managed_user.is_superuser) and get_active_admin_count() <= 1:
            messages.error(request, "No puedes desactivar el ultimo administrador activo.")
            return redirect("portfolio:user_management")

        managed_user.is_active = not managed_user.is_active
        managed_user.save(update_fields=["is_active"])
        state_label = "activado" if managed_user.is_active else "desactivado"
        messages.success(request, f"El usuario {managed_user.username} ha sido {state_label}.")
        return redirect("portfolio:user_management")

    def _promote_self_to_admin(self, request):
        if not is_user_management_recovery_mode():
            messages.error(request, "El modo recuperacion solo se activa cuando no queda ningun administrador.")
            return redirect("portfolio:user_management")

        user = request.user
        set_user_admin_flags(user, True)
        user.is_active = True
        user.save(update_fields=["is_staff", "is_superuser", "is_active"])
        messages.success(request, "Tu usuario se ha convertido en administrador y ya puedes gestionar accesos.")
        return redirect("portfolio:user_management")
