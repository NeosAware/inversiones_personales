from decimal import Decimal

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.shortcuts import redirect
from django.utils.dateparse import parse_date
from django.views.generic import TemplateView

from .forms import EquityDocumentImportForm, EquityPositionForm
from .models import EquityPosition
from .services import (
    build_equity_analysis_dashboard,
    EquityDocumentImportError,
    build_equity_history_cards,
    extract_equity_position_prefill,
    get_equity_company_catalog,
    sync_all_equities_market_data,
)


class EquityPositionListView(LoginRequiredMixin, TemplateView):
    template_name = "equities/equityposition_list.html"

    def _selected_period_bounds(self):
        start_date = parse_date(self.request.GET.get("period_start", "").strip()) if self.request.GET.get("period_start") else None
        end_date = parse_date(self.request.GET.get("period_end", "").strip()) if self.request.GET.get("period_end") else None
        if start_date and end_date and end_date < start_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date

    def _auto_sync_market_data(self):
        positions = list(EquityPosition.objects.all())
        if not positions or not getattr(settings, "EQUITIES_AUTO_SYNC_ON_VIEW", True):
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
        auto_sync = self._auto_sync_market_data()
        positions = list(EquityPosition.objects.prefetch_related("price_history"))
        selected_start_date, selected_end_date = self._selected_period_bounds()
        dashboard = build_equity_analysis_dashboard(
            positions,
            selected_start_date=selected_start_date,
            selected_end_date=selected_end_date,
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
        context["analysis_overview"] = dashboard["overview"]
        context["auto_sync"] = auto_sync
        context["selected_period_start"] = selected_start_date
        context["selected_period_end"] = selected_end_date
        context["position_form"] = kwargs.get("position_form", EquityPositionForm())
        context["document_form"] = kwargs.get("document_form", EquityDocumentImportForm())
        context["prefill_source_filename"] = kwargs.get("prefill_source_filename")
        context["equity_company_catalog"] = get_equity_company_catalog()
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "create_position")
        if action == "sync_market_data":
            return self._sync_market_data(request)
        if action == "prefill_from_document":
            return self._prefill_position_from_document(request)
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
                "benchmark_symbol": "ECB:M.S0.N.C_EUR1Y.E",
                "benchmark_name": "Euribor 12M",
            }
        if reference_profile == EquityPosition.ReferenceProfile.SPAIN_HOUSE_PRICE:
            return {
                "reference_profile": reference_profile,
                "benchmark_symbol": "EUROSTAT:prc_hpi_q:ES:TOTAL:I15_Q",
                "benchmark_name": "Precio vivienda Espana",
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

        if created:
            messages.success(request, f"Posicion {position.ticker} creada correctamente.")
        else:
            messages.success(request, f"Posicion {position.ticker} actualizada correctamente.")
            if duplicate_count > 1:
                messages.warning(
                    request,
                    f"Se han detectado {duplicate_count} posiciones repetidas para {position.ticker}. Se ha actualizado la mas reciente.",
                )
        return redirect("equities:list")
