from django.urls import path

from .views import (
    EquityOptimizationDownloadView,
    EquityOptimizationProgressView,
    EquityOptimizationReportView,
    EquityPositionListView,
    IbexEquityDetailView,
)


app_name = "equities"

urlpatterns = [
    path("ibex/<str:ticker>/", IbexEquityDetailView.as_view(), name="ibex_detail"),
    path("optimizations/<int:pk>/", EquityOptimizationReportView.as_view(), name="optimization_report"),
    path("optimizations/<int:pk>/download/", EquityOptimizationDownloadView.as_view(), name="optimization_download"),
    path("optimizations/<int:pk>/progress/", EquityOptimizationProgressView.as_view(), name="optimization_progress"),
    path("", EquityPositionListView.as_view(), name="list"),
]
