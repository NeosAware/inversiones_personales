import mimetypes
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse
from django.utils.http import content_disposition_header


class PortalLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True


def healthcheck_view(request):
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")


@login_required
def secure_media_download_view(request, path):
    try:
        if not default_storage.exists(path):
            raise Http404("Documento no encontrado.")
        file_handle = default_storage.open(path, "rb")
    except SuspiciousFileOperation as exc:
        raise Http404("Documento no encontrado.") from exc

    safe_filename = Path(path).name or "documento"
    content_type, _encoding = mimetypes.guess_type(safe_filename)
    response = FileResponse(
        file_handle,
        content_type=content_type or "application/octet-stream",
    )
    response["Content-Disposition"] = content_disposition_header(False, safe_filename)
    response["Cache-Control"] = "private, no-store, no-cache, max-age=0, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return response
