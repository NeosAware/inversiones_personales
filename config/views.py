from django.contrib.auth.views import LoginView
from django.http import HttpResponse


class PortalLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True


def healthcheck_view(request):
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")
