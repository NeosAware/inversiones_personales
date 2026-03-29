from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import LoginView
from django.http import HttpResponse


class PortalLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True


@login_not_required
def healthcheck_view(request):
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")
