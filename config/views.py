import mimetypes

from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse


class PortalLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True


def healthcheck_view(request):
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")


@login_required
def secure_media_download_view(request, path):
    if not default_storage.exists(path):
        raise Http404("Documento no encontrado.")

    content_type, _encoding = mimetypes.guess_type(path)
    file_handle = default_storage.open(path, "rb")
    response = FileResponse(
        file_handle,
        content_type=content_type or "application/octet-stream",
    )
    response["Content-Disposition"] = f'inline; filename="{path.rsplit("/", 1)[-1]}"'
    return response
