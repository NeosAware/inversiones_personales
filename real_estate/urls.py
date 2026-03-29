from django.urls import path

from .views import PropertyInvestmentListView


app_name = "real_estate"

urlpatterns = [
    path("", PropertyInvestmentListView.as_view(), name="list"),
]
