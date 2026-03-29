from django.urls import path

from .views import AdditivesHoldingListView


app_name = "neos_additives"

urlpatterns = [
    path("", AdditivesHoldingListView.as_view(), name="list"),
]
