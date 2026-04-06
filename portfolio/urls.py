from django.urls import path

from .views import PortfolioDashboardView, UserManagementView


app_name = "portfolio"

urlpatterns = [
    path("", PortfolioDashboardView.as_view(), name="dashboard"),
    path("usuarios/", UserManagementView.as_view(), name="user_management"),
]
