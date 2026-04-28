import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import TemplateView

from portfolio.user_management import can_user_manage_financial_data

from .forms import (
    VentureBalanceAnalysisForm,
    VentureDossierAnalysisForm,
    VentureInformaImportForm,
    VentureOpportunityForm,
    VentureWebDiscoveryForm,
)
from .models import VentureDiscoveryCandidate, VentureDocument, VentureOpportunity
from .services import (
    build_opportunity_seed_from_pdf,
    build_venture_study_context,
    discover_web_candidates,
    import_informa_report,
    promote_discovery_candidate,
    run_document_analysis,
    run_opportunity_documents_analysis,
)


logger = logging.getLogger(__name__)


class VentureOpportunityListView(LoginRequiredMixin, TemplateView):
    template_name = "venture_studies/ventureopportunity_list.html"

    analysis_kind_priority = {
        VentureDocument.DocumentKind.BALANCE: 0,
        VentureDocument.DocumentKind.INFORMA: 1,
        VentureDocument.DocumentKind.DOSSIER: 2,
        VentureDocument.DocumentKind.PITCH: 3,
        VentureDocument.DocumentKind.CONTRACT: 4,
        VentureDocument.DocumentKind.OTHER: 5,
    }

    def _sort_documents_for_analysis(self, documents):
        return sorted(
            documents,
            key=lambda item: (
                item.extraction_status != VentureDocument.ExtractionStatus.EXTRACTED,
                self.analysis_kind_priority.get(item.document_kind, 9),
                -item.uploaded_at.timestamp(),
            ),
        )

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

    def _posted_opportunity(self, request):
        opportunity_id = str(request.POST.get("opportunity") or "").strip()
        if not opportunity_id:
            return None
        return VentureOpportunity.objects.filter(pk=opportunity_id).first()

    def _context_with_posted_opportunity(self, request, **forms):
        opportunity = self._posted_opportunity(request)
        kwargs = dict(forms)
        if opportunity:
            kwargs["creating_new_company"] = False
            kwargs["selected_opportunity_override"] = opportunity
        return self.get_context_data(**kwargs)

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
        selected_documents = list(selected_opportunity.documents.all()) if selected_opportunity else []
        context["selected_documents"] = selected_documents
        context["selected_analysis_document"] = (
            self._sort_documents_for_analysis(selected_documents)[0]
            if selected_documents
            else None
        )
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
        context.setdefault("dossier_form", VentureDossierAnalysisForm())
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
        if action == "upload_dossier":
            return self._upload_dossier(request)
        if action == "upload_informa":
            return self._upload_informa(request)
        if action == "analyze_document":
            return self._analyze_existing_document(request)
        if action == "analyze_opportunity_documents":
            return self._analyze_opportunity_documents(request)
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
        if not opportunity_instance and request.FILES.get("file") and not request.POST.get("company_name", "").strip():
            return self._create_opportunity_from_initial_pdf(request)
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
        self._analyze_attached_dossier(request, opportunity)
        return self._redirect_to_company(opportunity.id)

    def _create_opportunity_from_initial_pdf(self, request):
        try:
            seed = build_opportunity_seed_from_pdf(
                request.FILES["file"],
                fallback_company_name=request.POST.get("company_name", ""),
            )
        except Exception as exc:
            logger.exception("Error creating venture opportunity seed from PDF upload")
            seed = {
                "company_name": "",
                "fields": {},
                "text": "",
                "error": str(exc),
            }
        if not seed["company_name"]:
            form = VentureOpportunityForm(request.POST)
            form.add_error("company_name", "No se ha podido detectar la empresa en el PDF. Escribe el nombre y vuelve a guardar.")
            if seed.get("error"):
                messages.warning(request, f"No se pudo leer el PDF automaticamente: {seed['error']}")
            context = self.get_context_data(form=form, creating_new_company=True)
            return self.render_to_response(context, status=400)

        data = request.POST.copy()
        data["company_name"] = seed["company_name"]
        for field_name, value in seed["fields"].items():
            if field_name in VentureOpportunityForm.Meta.fields and data.get(field_name) in (None, "") and value not in (None, ""):
                data[field_name] = str(value)

        form = VentureOpportunityForm(data)
        if not form.is_valid():
            context = self.get_context_data(form=form, creating_new_company=True)
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
            messages.success(request, f"La empresa {opportunity.company_name} se ha creado desde el PDF.")
        else:
            messages.success(request, f"La empresa {opportunity.company_name} se ha localizado y actualizado desde el PDF.")
        self._analyze_attached_dossier(request, opportunity, extracted_text=seed.get("text", ""))
        return self._redirect_to_company(opportunity.id)

    def _document_analysis_failed(self, request, document, exc):
        logger.exception("Error analysing venture document %s", document.pk)
        document.extraction_error = str(exc)[:1000]
        update_fields = ["extraction_error"]
        if not document.extracted_text:
            document.extraction_status = VentureDocument.ExtractionStatus.FAILED
            update_fields.append("extraction_status")
        document.save(update_fields=update_fields)
        messages.warning(
            request,
            (
                "El PDF se ha guardado, pero el analisis automatico no se ha podido completar. "
                "Revisa que el PDF tenga texto seleccionable o prueba con una version OCR."
            ),
        )
        return None

    def _analyze_attached_dossier(self, request, opportunity, *, extracted_text: str = ""):
        if "file" not in request.FILES:
            return None
        data = request.POST.copy()
        data["opportunity"] = str(opportunity.id)
        form = VentureDossierAnalysisForm(data, request.FILES)
        if not form.is_valid():
            messages.warning(
                request,
                "La empresa se ha guardado, pero el PDF financiero/comercial no se pudo analizar. Revisa que sea un PDF valido.",
            )
            return None

        document = form.save_document()
        if extracted_text:
            document.extracted_text = extracted_text
            document.extraction_status = VentureDocument.ExtractionStatus.EXTRACTED
            document.extraction_error = ""
            document.save(update_fields=["extracted_text", "extraction_status", "extraction_error"])
        try:
            snapshot = run_document_analysis(
                document,
                use_ai=form.cleaned_data.get("use_ai", True),
            )
        except Exception as exc:
            return self._document_analysis_failed(request, document, exc)
        messages.success(
            request,
            (
                f"PDF financiero/comercial de {document.opportunity.company_name} analizado por {snapshot.agent_label}. "
                f"Recomendacion: {snapshot.get_recommendation_display()} "
                f"y precio orientativo {snapshot.suggested_purchase_price or 0:.2f} EUR."
            ),
        )
        if form.cleaned_data.get("use_ai", True) and snapshot.agent_provider != "anthropic":
            messages.warning(
                request,
                "Claude no ha intervenido en este analisis. Revisa AI_LLM_PROVIDER=anthropic y ANTHROPIC_API_KEY si quieres lectura Claude.",
            )
        if document.extraction_status == VentureDocument.ExtractionStatus.FAILED:
            messages.warning(
                request,
                "El PDF se ha guardado, pero no se pudo extraer texto. Si es un escaneo, sube una version con OCR para que Claude pueda leerlo.",
            )
        return snapshot

    def _upload_balance(self, request):
        form = VentureBalanceAnalysisForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self._context_with_posted_opportunity(request, analysis_form=form)
            return self.render_to_response(context, status=400)

        document = form.save_document()
        try:
            snapshot = run_document_analysis(
                document,
                use_ai=form.cleaned_data.get("use_ai", True),
            )
        except Exception as exc:
            self._document_analysis_failed(request, document, exc)
            return self._redirect_to_company(document.opportunity_id)
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

    def _upload_dossier(self, request):
        form = VentureDossierAnalysisForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self._context_with_posted_opportunity(request, dossier_form=form)
            return self.render_to_response(context, status=400)

        document = form.save_document()
        try:
            snapshot = run_document_analysis(
                document,
                use_ai=form.cleaned_data.get("use_ai", True),
            )
        except Exception as exc:
            self._document_analysis_failed(request, document, exc)
            return self._redirect_to_company(document.opportunity_id)
        messages.success(
            request,
            (
                f"Dossier de {document.opportunity.company_name} analizado por {snapshot.agent_label}. "
                f"Recomendacion: {snapshot.get_recommendation_display()} "
                f"y precio orientativo {snapshot.suggested_purchase_price or 0:.2f} EUR."
            ),
        )
        if form.cleaned_data.get("use_ai", True) and snapshot.agent_provider != "anthropic":
            messages.warning(
                request,
                "Claude no ha intervenido en este analisis. Revisa AI_LLM_PROVIDER=anthropic y ANTHROPIC_API_KEY si quieres lectura Claude.",
            )
        if document.extraction_status == VentureDocument.ExtractionStatus.FAILED:
            messages.warning(
                request,
                "El PDF se ha guardado, pero no se pudo extraer texto. Si es un escaneo, sube una version con OCR para que Claude pueda leerlo.",
            )
        return self._redirect_to_company(document.opportunity_id)

    def _upload_informa(self, request):
        form = VentureInformaImportForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self._context_with_posted_opportunity(request, informa_form=form)
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
            context = self._context_with_posted_opportunity(request, informa_form=form)
            return self.render_to_response(context, status=400)

        opportunity = result["opportunity"]
        updated_labels = ", ".join(result["updated_fields"]) if result["updated_fields"] else "sin cambios en campos existentes"
        if result["created"]:
            messages.success(request, f"Informe Informa importado y empresa {opportunity.company_name} creada.")
        else:
            messages.success(request, f"Informe Informa importado para {opportunity.company_name}: {updated_labels}.")
        snapshot = None
        if result["document"].extraction_status == VentureDocument.ExtractionStatus.EXTRACTED:
            try:
                snapshot = run_document_analysis(
                    result["document"],
                    use_ai=form.cleaned_data.get("use_ai", True),
                )
            except Exception as exc:
                self._document_analysis_failed(request, result["document"], exc)
        if snapshot:
            messages.success(
                request,
                (
                    f"Informe Informa analizado por {snapshot.agent_label}. "
                    f"Recomendacion: {snapshot.get_recommendation_display()} "
                    f"y precio orientativo {snapshot.suggested_purchase_price or 0:.2f} EUR."
                ),
            )
            if form.cleaned_data.get("use_ai", True) and snapshot.agent_provider != "anthropic":
                messages.warning(
                    request,
                    "Claude no ha intervenido en este analisis. Revisa AI_LLM_PROVIDER=anthropic y ANTHROPIC_API_KEY si quieres lectura Claude.",
                )
        if result["document"].extraction_status == VentureDocument.ExtractionStatus.FAILED:
            messages.warning(request, "El PDF se ha guardado, pero no contiene texto extraible. Prueba con una version OCR.")
        return self._redirect_to_company(opportunity.id)

    def _analyze_existing_document(self, request):
        document_id = str(request.POST.get("document_id") or "").strip()
        document = get_object_or_404(
            VentureDocument.objects.select_related("opportunity"),
            pk=document_id,
        )
        try:
            snapshot = run_document_analysis(document, use_ai=True)
        except Exception as exc:
            self._document_analysis_failed(request, document, exc)
            return self._redirect_to_company(document.opportunity_id)
        messages.success(
            request,
            (
                f"Documento {document.title} analizado por {snapshot.agent_label}. "
                f"Recomendacion: {snapshot.get_recommendation_display()} "
                f"y precio orientativo {snapshot.suggested_purchase_price or 0:.2f} EUR."
            ),
        )
        if snapshot.agent_provider != "anthropic":
            messages.warning(
                request,
                "Claude no ha intervenido en este analisis. Revisa AI_LLM_PROVIDER=anthropic y ANTHROPIC_API_KEY si quieres lectura Claude.",
            )
        return self._redirect_to_company(document.opportunity_id)

    def _analyze_opportunity_documents(self, request):
        opportunity = get_object_or_404(
            VentureOpportunity.objects.prefetch_related("documents"),
            pk=request.POST.get("opportunity_id"),
        )
        documents = self._sort_documents_for_analysis(list(opportunity.documents.all()))
        if not documents:
            messages.warning(request, "Esta empresa todavia no tiene PDFs cargados para analizar.")
            return self._redirect_to_company(opportunity.id)
        try:
            snapshot = run_opportunity_documents_analysis(
                opportunity,
                documents,
                use_ai=True,
            )
        except Exception as exc:
            logger.exception("Error analysing venture opportunity documents %s", opportunity.pk)
            messages.warning(
                request,
                (
                    "Los documentos estan guardados, pero no se ha podido generar el analisis automatico. "
                    f"Detalle: {str(exc)[:220]}"
                ),
            )
            return self._redirect_to_company(opportunity.id)
        messages.success(
            request,
            (
                f"Analisis combinado de {opportunity.company_name} generado por {snapshot.agent_label}. "
                f"Recomendacion: {snapshot.get_recommendation_display()} "
                f"y precio orientativo {snapshot.suggested_purchase_price or 0:.2f} EUR."
            ),
        )
        if snapshot.agent_provider != "anthropic":
            messages.warning(
                request,
                "Claude no ha intervenido en este analisis. Revisa AI_LLM_PROVIDER=anthropic y ANTHROPIC_API_KEY si quieres lectura Claude.",
            )
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
        if result.get("signal", {}).get("available") is False and not result.get("candidates"):
            messages.warning(request, result["signal"].get("note") or "No se han encontrado candidatos web.")
        else:
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
