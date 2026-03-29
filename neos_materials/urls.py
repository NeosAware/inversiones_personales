from django.urls import path

from .views import MaterialsHoldingListView


app_name = "neos_materials"

urlpatterns = [
    path("", MaterialsHoldingListView.as_view(), name="list"),
]
