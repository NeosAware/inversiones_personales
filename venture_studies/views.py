from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import TemplateView

from portfolio.user_management import can_user_manage_financial_data

from .forms import VentureBalanceAnalysisForm, VentureInformaImportForm, VentureOpportunityForm, VentureWebDiscoveryForm
from .models import VentureDiscoveryCandidate, VentureDocument, VentureOpportunity
from .services import (
    build_venture_study_context,
    discover_web_candidates,
    import_informa_report,
    promote_discovery_candidate,
    run_document_analysis,
)


class VentureOpportunityListView(LoginRequiredMixin, TemplateView):
    template_name = "venture_studies/ventureopportunity_list.html"

    def _is_new_company_mode(self):
        return str(self.request.GET.get("new") or "").strip() in {"1", "true", "yes"}

    def _redirect_to_company(self, opportunity_id=None):
        url = reverse("venture_studies:list")
        if opportunity_id:
            return redirect(f"{url}?company={opportunity_id}")
        return redirect("venture_studies:list")

    def _selected_opportunity(self, opportunities):
        if self._is_new_company_mode():
            return None
        selected_id = str(self.request.GET.get("company") or "").strip()
        if selected_id:
            for opportunity in opportunities:
                if str(opportunity.id) == selected_id:
                    return opportunity
        return opportunities[0] if opportunities else None

    def _opportunity_form_data(self, post_data, opportunity=None):
        data = post_data.copy()
        if not opportunity:
            return data
        for field_name in VentureOpportunityForm.Meta.fields:
            if field_name in data:
                continue
            value = getattr(opportunity, field_name)
            if value is None:
                data[field_name] = ""
            elif hasattr(value, "isoformat"):
                data[field_name] = value.isoformat()
            else:
                data[field_name] = str(value)
        return data

    def get_context_data(self, **kwargs):
        creating_new_company = kwargs.pop("creating_new_company", None)
        selected_opportunity_override = kwargs.pop("selected_opportunity_override", None)
        context = super().get_context_data(**kwargs)
        opportunities = list(
            VentureOpportunity.objects.prefetch_related(
                "documents",
                "analysis_snapshots",
            )
        )
        selected_opportunity = None
        if selected_opportunity_override:
            selected_opportunity = next(
                (item for item in opportunities if item.id == selected_opportunity_override.id),
                selected_opportunity_override,
            )
        if not selected_opportunity:
            selected_opportunity = self._selected_opportunity(opportunities)
        if creating_new_company is None:
            creating_new_company = self._is_new_company_mode() or not selected_opportunity
        if creating_new_company:
            selected_opportunity = None
        context["page_title"] = "Radar de empresas no cotizadas"
        context["opportunities"] = opportunities
        context["selected_opportunity"] = selected_opportunity
        context["creating_new_company"] = creating_new_company
        context["selected_documents"] = list(selected_opportunity.documents.all()) if selected_opportunity else []
        context["selected_analyses"] = list(selected_opportunity.analysis_snapshots.all()) if selected_opportunity else []
        context["documents"] = list(VentureDocument.objects.select_related("opportunity").order_by("-uploaded_at", "-id")[:30])
        context["discovery_candidates"] = list(
            VentureDiscoveryCandidate.objects.select_related("promoted_opportunity").order_by(
                "status",
                "-score_pct",
                "-discovered_at",
            )[:30]
        )
        context["can_manage_finances"] = can_user_manage_financial_data(self.request.user)
        context.setdefault("form", VentureOpportunityForm())
        context.setdefault(
            "selected_company_form",
            VentureOpportunityForm(instance=selected_opportunity) if selected_opportunity else None,
        )
        context.setdefault("analysis_form", VentureBalanceAnalysisForm())
        context.setdefault("informa_form", VentureInformaImportForm())
        context.setdefault("discovery_form", VentureWebDiscoveryForm())
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
        if action == "delete_opportunity":
            return self._delete_opportunity(request)
        if action == "discover_web":
            return self._discover_web(request)
        if action == "promote_candidate":
            return self._promote_candidate(request)
        if action == "reject_candidate":
            return self._reject_candidate(request)

        opportunity_id = request.POST.get("opportunity_id", "").strip()
        opportunity_instance = get_object_or_404(VentureOpportunity, pk=opportunity_id) if opportunity_id else None
        form = VentureOpportunityForm(
            self._opportunity_form_data(request.POST, opportunity_instance),
            instance=opportunity_instance,
        )
        if not form.is_valid():
            context_key = "selected_company_form" if opportunity_instance else "form"
            context = self.get_context_data(
                **{context_key: form},
                creating_new_company=not opportunity_instance,
                selected_opportunity_override=opportunity_instance,
            )
            return self.render_to_response(context, status=400)

        defaults = {
            field_name: form.cleaned_data[field_name]
            for field_name in VentureOpportunityForm.Meta.fields
            if field_name != "company_name"
        }
        if opportunity_instance:
            opportunity = opportunity_instance
            opportunity.company_name = form.cleaned_data["company_name"]
            for field_name, value in defaults.items():
                setattr(opportunity, field_name, value)
            opportunity.save()
            created = False
        else:
            opportunity, created = VentureOpportunity.objects.update_or_create(
                company_name=form.cleaned_data["company_name"],
                defaults=defaults,
            )
        if created:
            messages.success(request, f"La empresa {opportunity.company_name} se ha incorporado al radar.")
        else:
            messages.success(request, f"La empresa {opportunity.company_name} se ha actualizado correctamente.")
        return self._redirect_to_company(opportunity.id)

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
        return self._redirect_to_company(document.opportunity_id)

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
        return self._redirect_to_company(opportunity.id)

    def _delete_opportunity(self, request):
        opportunity = get_object_or_404(VentureOpportunity, pk=request.POST.get("opportunity_id"))
        company_name = opportunity.company_name
        opportunity.delete()
        messages.success(request, f"{company_name} se ha eliminado del radar.")
        return redirect("venture_studies:list")

    def _discover_web(self, request):
        form = VentureWebDiscoveryForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(discovery_form=form)
            return self.render_to_response(context, status=400)
        result = discover_web_candidates(
            geography=form.cleaned_data["geography"],
            sector_focus=form.cleaned_data["sector_focus"],
            max_candidates=form.cleaned_data["max_candidates"],
        )
        messages.success(
            request,
            (
                f"Radar web actualizado: {result['created_count']} candidato(s) nuevo(s) "
                f"y {result['updated_count']} revisado(s)."
            ),
        )
        return redirect("venture_studies:list")

    def _promote_candidate(self, request):
        candidate = get_object_or_404(VentureDiscoveryCandidate, pk=request.POST.get("candidate_id"))
        opportunity = promote_discovery_candidate(candidate)
        messages.success(request, f"{opportunity.company_name} se ha incorporado al radar desde la vigilancia web.")
        return self._redirect_to_company(opportunity.id)

    def _reject_candidate(self, request):
        candidate = get_object_or_404(VentureDiscoveryCandidate, pk=request.POST.get("candidate_id"))
        candidate.status = VentureDiscoveryCandidate.Status.REJECTED
        candidate.save(update_fields=["status", "updated_at"])
        messages.success(request, f"{candidate.company_name} se ha descartado de la vigilancia web.")
        return redirect("venture_studies:list")
