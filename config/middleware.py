from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import resolve_url
from django.utils.deprecation import MiddlewareMixin


class GlobalLoginRequiredMiddleware(MiddlewareMixin):
    redirect_field_name = REDIRECT_FIELD_NAME

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.user.is_authenticated:
            return None

        if not getattr(view_func, "login_required", True):
            return None

        if self._is_public_path(request.path_info):
            return None

        return self.handle_no_permission(request)

    def _is_public_path(self, path):
        prefixes = [
            "/admin/",
            "/health/",
            settings.STATIC_URL,
            settings.MEDIA_URL,
        ]
        login_path = resolve_url(settings.LOGIN_URL)
        if path == login_path or path.startswith(f"{login_path}?"):
            return True
        return any(prefix and path.startswith(prefix) for prefix in prefixes)

    def handle_no_permission(self, request):
        path = request.build_absolute_uri()
        resolved_login_url = resolve_url(settings.LOGIN_URL)
        login_scheme, login_netloc = urlsplit(resolved_login_url)[:2]
        current_scheme, current_netloc = urlsplit(path)[:2]
        if (not login_scheme or login_scheme == current_scheme) and (
            not login_netloc or login_netloc == current_netloc
        ):
            path = request.get_full_path()

        return redirect_to_login(
            path,
            resolved_login_url,
            self.redirect_field_name,
        )


class PrivateDataNoCacheMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        user = getattr(request, "user", None)
        path = getattr(request, "path_info", "") or ""
        if not getattr(user, "is_authenticated", False):
            return response
        if path.startswith("/health/"):
            return response
        static_url = getattr(settings, "STATIC_URL", "") or ""
        media_url = getattr(settings, "MEDIA_URL", "") or ""
        if static_url and path.startswith(static_url):
            return response
        if media_url and path.startswith(media_url):
            return response

        response.setdefault("Cache-Control", "private, no-store, no-cache, max-age=0, must-revalidate")
        response.setdefault("Pragma", "no-cache")
        response.setdefault("Expires", "0")
        return response
