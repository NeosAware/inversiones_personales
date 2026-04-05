from django.urls import path

from .views import (
    BankBalanceListView,
    local_bridge_statement_import_view,
    robot_assistant_installer_view,
    robot_statement_import_view,
)


app_name = "banking"

urlpatterns = [
    path("", BankBalanceListView.as_view(), name="list"),
    path("robot/installer/", robot_assistant_installer_view, name="robot_installer"),
    path("robot/local-import/", local_bridge_statement_import_view, name="robot_local_import"),
    path("robot/upload/", robot_statement_import_view, name="robot_upload"),
]
