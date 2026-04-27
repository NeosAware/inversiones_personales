from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from portfolio.user_management import can_user_manage_financial_data

from .forms import VentureBalanceAnalysisForm, VentureInformaImportForm, VentureOpportunityForm
from .models import VentureDocument, VentureOpportunity
from .services import build_venture_study_context, import_informa_report, run_document_analysis


class VentureOpportunityListView(LoginRequiredMixin, TemplateView):
    template_name = "venture_studies/ventureopportunity_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        opportunities = list(
            VentureOpportunity.objects.prefetch_related(
                "documents",
                "analysis_snapshots",
            )
        )
        context["page_title"] = "Radar de empresas no cotizadas"
        context["opportunities"] = opportunities
        context["documents"] = list(VentureDocument.objects.select_related("opportunity").order_by("-uploaded_at", "-id")[:30])
        context["can_manage_finances"] = can_user_manage_financial_data(self.request.user)
        context.setdefault("form", VentureOpportunityForm())
        context.setdefault("analysis_form", VentureBalanceAnalysisForm())
        context.setdefault("informa_form", VentureInformaImportForm())
        context.update(build_venture_study_context(opportunities))
        return context

    def post(self, request, *args, **kwargs):
        if not can_user_manage_financial_data(request.user):
            messages.error(request, "Solo un administrador puede modificar el radar de empresas no cotizadas.")
            return redirect("venture_studies:list")

        action = request.POST.get("action", "save_opportunity")
        if action == "upload_balance":
            return self._upload_balance(request)
        if action == "upload_informa":
            return self._upload_informa(request)

        form = VentureOpportunityForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(form=form)
            return self.render_to_response(context, status=400)

        defaults = {
            field_name: form.cleaned_data[field_name]
            for field_name in VentureOpportunityForm.Meta.fields
            if field_name != "company_name"
        }
        opportunity, created = VentureOpportunity.objects.update_or_create(
            company_name=form.cleaned_data["company_name"],
            defaults=defaults,
        )
        if created:
            messages.success(request, f"La empresa {opportunity.company_name} se ha incorporado al radar.")
        else:
            messages.success(request, f"La empresa {opportunity.company_name} se ha actualizado correctamente.")
        return redirect("venture_studies:list")

    def _upload_balance(self, request):
        form = VentureBalanceAnalysisForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self.get_context_data(analysis_form=form)
            return self.render_to_response(context, status=400)

        document = form.save_document()
        snapshot = run_document_analysis(
            document,
            use_ai=form.cleaned_data.get("use_ai", True),
        )
        messages.success(
            request,
            (
                f"Balance de {document.opportunity.company_name} analizado. "
                f"Recomendacion: {snapshot.get_recommendation_display()} "
                f"y precio orientativo {snapshot.suggested_purchase_price or 0:.2f} EUR."
            ),
        )
        if document.extraction_status == VentureDocument.ExtractionStatus.FAILED:
            messages.warning(
                request,
                "El PDF se ha guardado, pero no se pudo extraer texto. Si es un escaneo, sube una version con OCR para mejorar el analisis.",
            )
        return redirect("venture_studies:list")

    def _upload_informa(self, request):
        form = VentureInformaImportForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self.get_context_data(informa_form=form)
            return self.render_to_response(context, status=400)

        try:
            result = import_informa_report(
                form.cleaned_data["file"],
                selected_opportunity=form.cleaned_data.get("opportunity"),
                title=form.cleaned_data.get("title", ""),
                document_date=form.cleaned_data.get("document_date"),
                overwrite_existing=form.cleaned_data.get("overwrite_existing", False),
            )
        except Exception as exc:
            messages.error(request, f"No se ha podido importar el informe Informa: {exc}")
            context = self.get_context_data(informa_form=form)
            return self.render_to_response(context, status=400)

        opportunity = result["opportunity"]
        updated_labels = ", ".join(result["updated_fields"]) if result["updated_fields"] else "sin cambios en campos existentes"
        if result["created"]:
            messages.success(request, f"Informe Informa importado y empresa {opportunity.company_name} creada.")
        else:
            messages.success(request, f"Informe Informa importado para {opportunity.company_name}: {updated_labels}.")
        if result["document"].extraction_status == VentureDocument.ExtractionStatus.FAILED:
            messages.warning(request, "El PDF se ha guardado, pero no contiene texto extraible. Prueba con una version OCR.")
        return redirect("venture_studies:list")
