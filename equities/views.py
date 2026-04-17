from decimal import Decimal

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.generic import TemplateView, View

from .forms import EquityAllocationOptimizerForm, EquityClosePositionForm, EquityDocumentImportForm, EquityOptimizationRunForm, EquityPositionForm
from .models import EquityClosedPosition, EquityOptimizationRun, EquityPosition
from .nightly_analysis import (
    build_dashboard_from_nightly_cache,
    build_ibex_recommendation_date_map,
    build_nightly_analysis_status,
    capture_purchase_forecast_baseline,
    load_cached_ibex_card,
)
from .optimization_runs import (
    build_fallback_report_pdf_html,
    launch_equity_optimization_run_pair,
    render_report_pdf,
    resume_equity_optimization_runs,
)
from .services import (
    EURIBOR_REFERENCE_NAME,
    EURIBOR_REFERENCE_SYMBOL,
    SPAIN_ELECTRICITY_DEMAND_NAME,
    SPAIN_ELECTRICITY_DEMAND_SYMBOL,
    SPAIN_GAS_CONSUMPTION_NAME,
    SPAIN_GAS_CONSUMPTION_SYMBOL,
    SPAIN_HOUSE_PRICE_NAME,
    SPAIN_HOUSE_PRICE_SYMBOL,
    build_equity_analysis_dashboard,
    build_equity_allocation_plan,
    build_equity_investment_journey_context,
    build_equity_ticket_tracking_context,
    build_ibex_universe_card,
    archive_equity_position_sale,
    capture_equity_ticket_snapshots,
    find_ibex_universe_company,
    EquityDocumentImportError,
    build_equity_history_cards,
    extract_equity_position_prefill,
    get_equity_company_catalog,
    get_equity_optimizer_sector_choices,
    sync_equity_market_data,
    sync_all_equities_market_data,
)


class EquityPeriodBoundsMixin:
    def _selected_period_bounds(self):
        start_date = parse_date(self.request.GET.get("period_start", "").strip()) if self.request.GET.get("period_start") else None
        end_date = parse_date(self.request.GET.get("period_end", "").strip()) if self.request.GET.get("period_end") else None
        if start_date and end_date and end_date < start_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date


class EquityPositionListView(LoginRequiredMixin, EquityPeriodBoundsMixin, TemplateView):
    template_name = "equities/equityposition_list.html"

    def _optimizer_requested(self) -> bool:
        return bool(self.request.GET.getlist("selected_sectors")) or any(
            self.request.GET.get(key)
            for key in ("total_investment", "max_company_pct", "max_total_positions", "max_sector_positions")
        )

    def _auto_sync_market_data(self, active_run_exists: bool = False):
        positions = list(EquityPosition.objects.all())
        if (
            not positions
            or not getattr(settings, "EQUITIES_AUTO_SYNC_ON_VIEW", True)
            or self._optimizer_requested()
            or active_run_exists
        ):
            return {
                "attempted": False,
                "updated_count": 0,
                "failed": [],
            }

        results = sync_all_equities_market_data(positions)
        return {
            "attempted": True,
            "updated_count": sum(1 for _, error in results if error is None),
            "failed": [f"{position.ticker}: {error}" for position, error in results if error],
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        resume_equity_optimization_runs()
        optimization_runs = list(EquityOptimizationRun.objects.select_related("requested_by")[:40])
        active_optimization_runs = [
            run for run in optimization_runs if run.status in {EquityOptimizationRun.Status.PENDING, EquityOptimizationRun.Status.RUNNING}
        ]
        auto_sync = self._auto_sync_market_data(active_run_exists=bool(active_optimization_runs))
        positions = list(EquityPosition.objects.prefetch_related("price_history"))
        closed_positions = list(EquityClosedPosition.objects.all())
        selected_start_date, selected_end_date = self._selected_period_bounds()
        optimizer_requested = self._optimizer_requested()
        defer_ibex_analysis = bool(active_optimization_runs) and not optimizer_requested
        include_ibex_universe = (
            False
            if defer_ibex_analysis
            else (True if optimizer_requested else getattr(settings, "EQUITIES_IBEX_UNIVERSE_ANALYSIS", True))
        )
        dashboard = build_dashboard_from_nightly_cache(
            positions,
            include_ibex_universe=include_ibex_universe,
            selected_start_date=selected_start_date,
            selected_end_date=selected_end_date,
        )
        if dashboard is None:
            dashboard = build_equity_analysis_dashboard(
                positions,
                selected_start_date=selected_start_date,
                selected_end_date=selected_end_date,
                include_ibex_universe=include_ibex_universe,
                ibex_company_limit=(
                    None
                    if optimizer_requested
                    else (getattr(settings, "EQUITIES_IBEX_UNIVERSE_LIMIT", 0) or None)
                ),
            )
        context["page_title"] = "Acciones cotizadas"
        context["positions"] = positions
        context["summary"] = {
            "owned_invested_amount": dashboard["overview"]["invested_amount"],
            "owned_current_value": dashboard["overview"]["current_value"],
            "owned_annual_income": dashboard["overview"]["net_annual_income_total"],
            "owned_positions_count": dashboard["overview"]["owned_positions_count"],
            "watchlist_positions_count": dashboard["overview"]["watchlist_positions_count"],
            "synced_positions": sum((1 for position in positions if position.last_synced_at), 0),
        }
        context["history_cards"] = dashboard["history_cards"]
        context["owned_positions"] = dashboard["owned_positions"]
        context["watchlist_positions"] = dashboard["watchlist_positions"]
        context["owned_history_cards"] = dashboard["owned_history_cards"]
        context["watchlist_history_cards"] = dashboard["watchlist_history_cards"]
        context["decision_rows"] = dashboard["decision_rows"]
        ibex_recommendation_dates = build_ibex_recommendation_date_map(
            [row["ticker"] for row in dashboard["ibex_universe_rows"]]
        )
        context["ibex_universe_rows"] = [
            {
                **row,
                **ibex_recommendation_dates.get(row["ticker"], {}),
                "detail_url": reverse("equities:ibex_detail", kwargs={"ticker": row["ticker"]}),
            }
            for row in dashboard["ibex_universe_rows"]
        ]
        context["investment_journey"] = build_equity_investment_journey_context(positions, closed_positions)
        context["ibex_universe_summary"] = dashboard["ibex_universe_summary"]
        context["ibex_analysis_deferred"] = defer_ibex_analysis
        context["tracked_reference_rows"] = dashboard["tracked_reference_rows"]
        context["reference_guide_rows"] = dashboard["reference_guide_rows"]
        context["reference_guide_summary"] = dashboard["reference_guide_summary"]
        context["analysis_overview"] = dashboard["overview"]
        context["nightly_analysis"] = build_nightly_analysis_status(
            positions,
            cache_available=bool(dashboard.get("nightly_analysis", {}).get("available")),
        )
        context["auto_sync"] = auto_sync
        context["selected_period_start"] = selected_start_date
        context["selected_period_end"] = selected_end_date
        context["position_form"] = kwargs.get("position_form", EquityPositionForm())
        context["document_form"] = kwargs.get("document_form", EquityDocumentImportForm())
        optimizer_sector_choices = get_equity_optimizer_sector_choices()
        optimizer_default_total = dashboard["overview"]["current_value"] or dashboard["overview"]["invested_amount"] or Decimal("100000")
        optimizer_run_form = kwargs.get(
            "optimizer_run_form",
            EquityOptimizationRunForm(
                default_total_investment=optimizer_default_total,
                sector_choices=optimizer_sector_choices,
            ),
        )
        optimizer_form = kwargs.get(
            "optimizer_form",
            EquityAllocationOptimizerForm(
                self.request.GET or None,
                default_total_investment=optimizer_default_total,
                sector_choices=optimizer_sector_choices,
            ),
        )
        optimizer_plan = None
        if optimizer_form.is_valid():
            optimizer_plan = build_equity_allocation_plan(
                dashboard["optimizer_cards"],
                optimizer_form.cleaned_data["total_investment"],
                optimizer_form.cleaned_data["max_company_pct"],
                optimizer_form.cleaned_data["max_total_positions"],
                optimizer_form.cleaned_data["max_sector_positions"],
                selected_sectors=optimizer_form.cleaned_data["selected_sectors"],
            )
        context["optimizer_form"] = optimizer_form
        context["optimizer_run_form"] = optimizer_run_form
        context["optimizer_plan"] = optimizer_plan
        context["optimization_runs"] = optimization_runs
        context["active_optimization_runs"] = active_optimization_runs
        context["latest_completed_optimization"] = next(
            (run for run in optimization_runs if run.status == EquityOptimizationRun.Status.COMPLETED and run.summary_data),
            None,
        )
        context["prefill_source_filename"] = kwargs.get("prefill_source_filename")
        context["equity_company_catalog"] = get_equity_company_catalog()
        context["today"] = timezone.localdate()
        if not context["nightly_analysis"]["available"]:
            capture_equity_ticket_snapshots(dashboard["owned_history_cards"])
        context["ticket_tracking"] = build_equity_ticket_tracking_context(dashboard["owned_history_cards"])
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "create_position")
        if action == "sync_market_data":
            return self._sync_market_data(request)
        if action == "prefill_from_document":
            return self._prefill_position_from_document(request)
        if action == "change_reference":
            return self._change_reference(request)
        if action == "delete_position":
            return self._delete_position(request)
        if action == "close_position":
            return self._close_position(request)
        if action == "launch_optimizer_run":
            return self._launch_optimizer_run(request)
        if action == "delete_optimization_run":
            return self._delete_optimization_run(request)
        return self._save_position(request)

    def _sync_market_data(self, request):
        positions = list(EquityPosition.objects.all())
        results = sync_all_equities_market_data(positions)
        updated = sum(1 for _, error in results if error is None)
        failed = [f"{position.ticker}: {error}" for position, error in results if error]

        if updated:
            messages.success(request, f"Datos de mercado actualizados para {updated} posicion(es).")
        for failure in failed:
            messages.warning(request, failure)

        return redirect("equities:list")

    def _prefill_position_from_document(self, request):
        form = EquityDocumentImportForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self.get_context_data(document_form=form)
            return self.render_to_response(context, status=400)

        document = form.cleaned_data["document"]
        try:
            prefill = extract_equity_position_prefill(
                document,
                default_broker=form.cleaned_data["default_broker"],
                default_ownership_category=form.cleaned_data["default_ownership_category"],
            )
        except EquityDocumentImportError as exc:
            messages.error(request, str(exc))
            context = self.get_context_data(document_form=form)
            return self.render_to_response(context, status=400)

        messages.success(
            request,
            f"Formulario rellenado con {len(prefill.detected_fields)} dato(s) detectado(s) en {document.name}.",
        )
        if prefill.candidate_count > 1:
            messages.info(
                request,
                "Se han detectado varias posiciones en el documento. Se ha precargado la que parecia mas completa.",
            )

        context = self.get_context_data(
            position_form=EquityPositionForm(initial=prefill.data),
            document_form=EquityDocumentImportForm(
                initial={
                    "default_broker": form.cleaned_data["default_broker"],
                    "default_ownership_category": form.cleaned_data["default_ownership_category"],
                }
            ),
            prefill_source_filename=document.name,
        )
        return self.render_to_response(context)

    def _resolve_reference_fields(self, cleaned_data):
        reference_profile = cleaned_data["reference_profile"]
        benchmark_symbol = cleaned_data["benchmark_symbol"]
        benchmark_name = cleaned_data["benchmark_name"]
        if reference_profile == EquityPosition.ReferenceProfile.EURIBOR_12M:
            return {
                "reference_profile": reference_profile,
                "benchmark_symbol": EURIBOR_REFERENCE_SYMBOL,
                "benchmark_name": EURIBOR_REFERENCE_NAME,
            }
        if reference_profile == EquityPosition.ReferenceProfile.SPAIN_HOUSE_PRICE:
            return {
                "reference_profile": reference_profile,
                "benchmark_symbol": SPAIN_HOUSE_PRICE_SYMBOL,
                "benchmark_name": SPAIN_HOUSE_PRICE_NAME,
            }
        if reference_profile == EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND:
            return {
                "reference_profile": reference_profile,
                "benchmark_symbol": SPAIN_ELECTRICITY_DEMAND_SYMBOL,
                "benchmark_name": SPAIN_ELECTRICITY_DEMAND_NAME,
            }
        if reference_profile == EquityPosition.ReferenceProfile.SPAIN_GAS_CONSUMPTION:
            return {
                "reference_profile": reference_profile,
                "benchmark_symbol": SPAIN_GAS_CONSUMPTION_SYMBOL,
                "benchmark_name": SPAIN_GAS_CONSUMPTION_NAME,
            }
        return {
            "reference_profile": reference_profile,
            "benchmark_symbol": benchmark_symbol,
            "benchmark_name": benchmark_name,
        }

    def _save_position(self, request):
        form = EquityPositionForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(position_form=form)
            return self.render_to_response(context, status=400)

        ticker = form.cleaned_data["ticker"]
        broker = form.cleaned_data["broker"]
        ownership_category = form.cleaned_data["ownership_category"]
        position_kind = form.cleaned_data["position_kind"]
        reference_defaults = self._resolve_reference_fields(form.cleaned_data)
        defaults = {
            "position_kind": position_kind,
            "company_name": form.cleaned_data["company_name"],
            "quote_symbol": form.cleaned_data["quote_symbol"],
            **reference_defaults,
            "trade_channel": form.cleaned_data["trade_channel"],
            "opened_on": form.cleaned_data["opened_on"],
            "shares": form.cleaned_data["shares"],
            "average_cost_per_share": form.cleaned_data["average_cost_per_share"],
            "current_price_per_share": form.cleaned_data["current_price_per_share"],
            "annual_dividend_income": form.cleaned_data["annual_dividend_income"],
            "annual_maintenance_cost": form.cleaned_data["annual_maintenance_cost"],
            "notes": form.cleaned_data["notes"],
        }
        matches = EquityPosition.objects.filter(
            broker=broker,
            ticker=ticker,
            ownership_category=ownership_category,
            position_kind=position_kind,
        )
        if matches.exists():
            position = matches.order_by("-updated_at", "-id").first()
            for field_name, value in defaults.items():
                setattr(position, field_name, value)
            position.save(update_fields=[*defaults.keys(), "updated_at"])
            created = False
            duplicate_count = matches.count()
        else:
            position = EquityPosition.objects.create(
                broker=broker,
                ticker=ticker,
                ownership_category=ownership_category,
                **defaults,
            )
            created = True
            duplicate_count = 0

        purchase_baseline = None
        if position.is_owned:
            purchase_baseline = capture_purchase_forecast_baseline(
                position,
                baseline_date=position.opened_on or timezone.localdate(),
            )

        if created:
            messages.success(request, f"Posicion {position.ticker} creada correctamente.")
        else:
            messages.success(request, f"Posicion {position.ticker} actualizada correctamente.")
            if duplicate_count > 1:
                messages.warning(
                    request,
                    f"Se han detectado {duplicate_count} posiciones repetidas para {position.ticker}. Se ha actualizado la mas reciente.",
                )
        if position.is_owned:
            if purchase_baseline is not None:
                messages.info(
                    request,
                    f"Se ha guardado la foto de compra de {position.ticker} con el analisis nocturno del {purchase_baseline.source_analysis_date:%Y-%m-%d}.",
                )
            else:
                messages.warning(
                    request,
                    f"No habia analisis nocturno disponible para guardar la foto de compra de {position.ticker}.",
                )
        return redirect("equities:list")

    def _change_reference(self, request):
        position_id = request.POST.get("position_id", "").strip()
        if not position_id:
            messages.error(request, "No se ha encontrado la posicion a actualizar.")
            return redirect("equities:list")

        try:
            position = EquityPosition.objects.get(pk=position_id)
        except EquityPosition.DoesNotExist:
            messages.error(request, "La posicion ya no existe.")
            return redirect("equities:list")

        position.reference_profile = request.POST.get("reference_profile", position.reference_profile).strip()
        position.benchmark_symbol = request.POST.get("benchmark_symbol", position.benchmark_symbol).strip()
        position.benchmark_name = request.POST.get("benchmark_name", position.benchmark_name).strip()
        position.save(update_fields=["reference_profile", "benchmark_symbol", "benchmark_name", "updated_at"])

        try:
            sync_equity_market_data(position)
            messages.success(request, f"Referencia de {position.ticker} actualizada a {position.benchmark_name}.")
        except Exception as exc:
            messages.warning(
                request,
                f"Se ha cambiado la referencia de {position.ticker}, pero no se pudo refrescar el historico: {exc}",
            )

        return redirect("equities:list")

    def _delete_position(self, request):
        position_id = request.POST.get("position_id", "").strip()
        if not position_id:
            messages.error(request, "No se ha encontrado la posicion a eliminar.")
            return redirect("equities:list")

        try:
            position = EquityPosition.objects.get(pk=position_id)
        except EquityPosition.DoesNotExist:
            messages.info(request, "La posicion ya no existe.")
            return redirect("equities:list")

        ticker = position.ticker
        company_name = position.company_name
        was_owned = position.is_owned
        position.delete()
        if was_owned:
            messages.success(request, f"{ticker} - {company_name} se ha eliminado de la lista de acciones.")
            return HttpResponseRedirect(f"{reverse('equities:list')}#equity-journey")
        messages.success(
            request,
            f"{ticker} - {company_name} ya no esta en seguimiento. Seguira apareciendo solo en el radar IBEX.",
        )
        return HttpResponseRedirect(f"{reverse('equities:list')}#equity-ibex")

    def _close_position(self, request):
        position_id = request.POST.get("position_id", "").strip()
        if not position_id:
            messages.error(request, "No se ha encontrado la posicion a vender.")
            return redirect("equities:list")

        try:
            position = EquityPosition.objects.get(pk=position_id)
        except EquityPosition.DoesNotExist:
            messages.info(request, "La posicion ya no existe.")
            return redirect("equities:list")

        if not position.is_owned:
            messages.error(request, "Solo puedes registrar una venta sobre posiciones compradas.")
            return redirect("equities:list")

        form = EquityClosePositionForm(request.POST)
        if not form.is_valid():
            error_text = " ".join(error for errors in form.errors.values() for error in errors)
            messages.error(request, error_text or "No se ha podido registrar la venta.")
            return redirect("equities:list")

        closed_on = form.cleaned_data["closed_on"]
        if position.opened_on and closed_on < position.opened_on:
            messages.error(request, "La fecha de venta no puede ser anterior a la fecha de compra.")
            return redirect("equities:list")

        archived = archive_equity_position_sale(
            position,
            closed_on=closed_on,
            sale_price_per_share=form.cleaned_data["sale_price_per_share"],
            notes=form.cleaned_data["notes"],
        )
        messages.success(
            request,
            f"Venta registrada para {archived.ticker}. Resultado neto {archived.net_result:.2f} EUR y margen acumulado {archived.cumulative_margin_pct:.2f} %. La veras en Ventas dentro del cuadro de gestion.",
        )
        return HttpResponseRedirect(f"{reverse('equities:list')}#equity-journey")

    def _launch_optimizer_run(self, request):
        positions = list(EquityPosition.objects.all())
        optimizer_default_total = sum((position.current_value for position in positions if position.is_owned), Decimal("0.00")) or sum(
            (position.invested_amount for position in positions if position.is_owned),
            Decimal("0.00"),
        ) or Decimal("100000")
        form = EquityOptimizationRunForm(
            request.POST,
            default_total_investment=optimizer_default_total,
            sector_choices=get_equity_optimizer_sector_choices(),
        )
        if not form.is_valid():
            context = self.get_context_data(optimizer_run_form=form)
            return self.render_to_response(context, status=400)

        runs = launch_equity_optimization_run_pair(
            total_investment=form.cleaned_data["total_investment"],
            max_company_pct=form.cleaned_data["max_company_pct"],
            max_total_positions=form.cleaned_data["max_total_positions"],
            max_sector_positions=form.cleaned_data["max_sector_positions"],
            selected_sectors=form.cleaned_data["selected_sectors"],
            requested_by=request.user if request.user.is_authenticated else None,
            reference_label=form.cleaned_data["reference_label"],
            restrictions_note=form.cleaned_data["restrictions_note"],
        )
        if all(run.status == EquityOptimizationRun.Status.COMPLETED for run in runs):
            messages.success(
                request,
                "Se han guardado dos optimizaciones hermanas: 12M principal y 5A principal.",
            )
        else:
            messages.success(
                request,
                "Se han lanzado dos optimizaciones en segundo plano: 12M principal y 5A principal.",
            )
        return redirect(f"{reverse('equities:list')}?optimizer_status=1#equity-optimizer")

    def _delete_optimization_run(self, request):
        run_id = request.POST.get("run_id", "").strip()
        if not run_id:
            messages.error(request, "No se ha encontrado la optimizacion a borrar.")
            return HttpResponseRedirect(f"{reverse('equities:list')}#equity-optimizer")

        try:
            run = EquityOptimizationRun.objects.get(pk=run_id)
        except EquityOptimizationRun.DoesNotExist:
            messages.info(request, "La optimizacion ya no existe en el historico.")
            return HttpResponseRedirect(f"{reverse('equities:list')}#equity-optimizer")

        if run.status not in {EquityOptimizationRun.Status.COMPLETED, EquityOptimizationRun.Status.FAILED}:
            messages.error(request, "Solo puedes borrar optimizaciones ya cerradas.")
            return HttpResponseRedirect(f"{reverse('equities:list')}#equity-optimizer")

        run_label = run.display_label
        run.delete()
        messages.success(request, f"Optimizacion {run_label} eliminada del historico.")
        return HttpResponseRedirect(f"{reverse('equities:list')}#equity-optimizer")


class IbexEquityDetailView(LoginRequiredMixin, EquityPeriodBoundsMixin, TemplateView):
    template_name = "equities/ibex_equity_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        positions = list(EquityPosition.objects.prefetch_related("price_history"))
        selected_start_date, selected_end_date = self._selected_period_bounds()
        company, workbook_snapshot = find_ibex_universe_company(self.kwargs.get("ticker", ""))
        if not company:
            raise Http404("No se ha encontrado ese valor del IBEX.")

        card = load_cached_ibex_card(
            self.kwargs.get("ticker", ""),
            positions,
            selected_start_date=selected_start_date,
            selected_end_date=selected_end_date,
        )
        if card is None:
            try:
                card = build_ibex_universe_card(
                    company,
                    positions,
                    selected_start_date=selected_start_date,
                    selected_end_date=selected_end_date,
                    workbook_snapshot=workbook_snapshot,
                )
            except Exception as exc:
                raise Http404(f"No se ha podido construir el analisis de {company.get('company_name') or company.get('ticker')}: {exc}") from exc

        context["page_title"] = card["position"].company_name
        context["card"] = card
        context["company"] = company
        context["selected_period_start"] = selected_start_date
        context["selected_period_end"] = selected_end_date
        return context


class EquityOptimizationReportView(LoginRequiredMixin, View):
    def get(self, request, pk, *args, **kwargs):
        resume_equity_optimization_runs()
        run = get_object_or_404(EquityOptimizationRun, pk=pk)
        if run.status != EquityOptimizationRun.Status.COMPLETED or not run.report_html:
            messages.info(request, f"La optimizacion {run.reference_code} todavia no esta lista.")
            return redirect(f"{reverse('equities:list')}#equity-optimizer")
        return HttpResponse(run.report_html)


class EquityOptimizationDownloadView(LoginRequiredMixin, View):
    def get(self, request, pk, *args, **kwargs):
        run = get_object_or_404(EquityOptimizationRun, pk=pk)
        if run.status != EquityOptimizationRun.Status.COMPLETED or not run.report_html:
            messages.info(request, f"La optimizacion {run.reference_code} todavia no esta lista para descargar.")
            return redirect(f"{reverse('equities:list')}#equity-optimizer")
        last_error = None
        pdf_bytes = None
        pdf_sources = []
        if run.report_pdf_html:
            pdf_sources.append(run.report_pdf_html)
        pdf_sources.append(build_fallback_report_pdf_html(run))
        for pdf_source in pdf_sources:
            try:
                pdf_bytes = render_report_pdf(pdf_source)
                break
            except Exception as exc:
                last_error = exc
        if pdf_bytes is None:
            messages.error(request, f"No se ha podido generar el PDF de {run.reference_code}: {last_error}")
            return redirect(f"{reverse('equities:list')}#equity-optimizer")
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{run.reference_code.lower()}.pdf"'
        return response


class EquityOptimizationHtmlDownloadView(LoginRequiredMixin, View):
    def get(self, request, pk, *args, **kwargs):
        run = get_object_or_404(EquityOptimizationRun, pk=pk)
        if run.status != EquityOptimizationRun.Status.COMPLETED or not run.report_html:
            messages.info(request, f"La optimizacion {run.reference_code} todavia no esta lista para descargar.")
            return redirect(f"{reverse('equities:list')}#equity-optimizer")
        response = HttpResponse(run.report_html, content_type="text/html; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{run.reference_code.lower()}.html"'
        return response


class EquityOptimizationProgressView(LoginRequiredMixin, View):
    def get(self, request, pk, *args, **kwargs):
        resume_equity_optimization_runs()
        run = get_object_or_404(EquityOptimizationRun, pk=pk)
        payload = {
            "id": run.id,
            "reference_code": run.reference_code,
            "label": run.display_label,
            "status": run.status,
            "status_label": run.get_status_display(),
            "finished": run.status in {EquityOptimizationRun.Status.COMPLETED, EquityOptimizationRun.Status.FAILED},
            "status_note": run.status_note,
            "created_at_label": run.created_at.strftime("%Y-%m-%d %H:%M"),
            "completed_at_label": run.completed_at.strftime("%Y-%m-%d %H:%M") if run.completed_at else "",
            "progress": run.progress_data or {},
            "summary": run.summary_data or {},
            "error_message": run.error_message,
            "report_url": reverse("equities:optimization_report", args=[run.id]) if run.report_html else "",
            "download_url": reverse("equities:optimization_download", args=[run.id]) if run.report_html else "",
            "download_html_url": reverse("equities:optimization_download_html", args=[run.id]) if run.report_html else "",
        }
        return JsonResponse(payload)
