from django.urls import path

from .views import BankBalanceListView, BankConnectionCallbackView


app_name = "banking"

urlpatterns = [
    path("", BankBalanceListView.as_view(), name="list"),
    path("connections/callback/", BankConnectionCallbackView.as_view(), name="connection_callback"),
]
