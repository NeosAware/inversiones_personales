from django.urls import path

from .views import VentureOpportunityListView


app_name = "venture_studies"

urlpatterns = [
    path("", VentureOpportunityListView.as_view(), name="list"),
]
