from django.urls import path

from .views import PortfolioDashboardView


app_name = "portfolio"

urlpatterns = [
    path("", PortfolioDashboardView.as_view(), name="dashboard"),
]
