from django.urls import path

from .views import VentureOpportunityListView, download_analysis_pdf, download_document_pdf, download_opportunity_report_pdf


app_name = "venture_studies"

urlpatterns = [
    path("", VentureOpportunityListView.as_view(), name="list"),
    path("analisis/<int:analysis_id>/pdf/", download_analysis_pdf, name="analysis_pdf"),
    path("empresas/<int:opportunity_id>/informe/pdf/", download_opportunity_report_pdf, name="opportunity_pdf"),
    path("documentos/<int:document_id>/pdf/", download_document_pdf, name="document_pdf"),
]
