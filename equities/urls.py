from django.urls import path

from .views import EquityPositionListView


app_name = "equities"

urlpatterns = [
    path("", EquityPositionListView.as_view(), name="list"),
]
