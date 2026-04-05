from django.urls import path

from .views import BankBalanceListView, robot_statement_import_view


app_name = "banking"

urlpatterns = [
    path("", BankBalanceListView.as_view(), name="list"),
    path("robot/upload/", robot_statement_import_view, name="robot_upload"),
]
