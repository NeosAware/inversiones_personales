from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from .views import PortalLoginView, healthcheck_view


urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", PortalLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("health/", healthcheck_view, name="healthcheck"),
    path("", include("portfolio.urls")),
    path("banking/", include("banking.urls")),
    path("equities/", include("equities.urls")),
    path("neos-additives/", include("neos_additives.urls")),
    path("neos-ceramica/", include("neos_ceramica.urls")),
    path("neos-materials/", include("neos_materials.urls")),
    path("real-estate/", include("real_estate.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
