from django.urls import path

from .views import EquityPositionListView, IbexEquityDetailView


app_name = "equities"

urlpatterns = [
    path("ibex/<str:ticker>/", IbexEquityDetailView.as_view(), name="ibex_detail"),
    path("", EquityPositionListView.as_view(), name="list"),
]
