from django.urls import path

from .views import CashflowManagementView, PortfolioDashboardView, UserManagementView


app_name = "portfolio"

urlpatterns = [
    path("", PortfolioDashboardView.as_view(), name="dashboard"),
    path("investment/", CashflowManagementView.as_view(), name="investment"),
    path("management/cashflow/", CashflowManagementView.as_view(), name="cashflow_management"),
    path("usuarios/", UserManagementView.as_view(), name="user_management"),
]
