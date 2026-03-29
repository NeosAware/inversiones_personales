from django.urls import path

from .views import BankBalanceListView


app_name = "banking"

urlpatterns = [
    path("", BankBalanceListView.as_view(), name="list"),
]
