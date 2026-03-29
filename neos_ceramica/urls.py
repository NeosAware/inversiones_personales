from django.urls import path

from .views import CeramicaHoldingListView


app_name = "neos_ceramica"

urlpatterns = [
    path("", CeramicaHoldingListView.as_view(), name="list"),
]
