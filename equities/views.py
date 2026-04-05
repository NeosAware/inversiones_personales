from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from .forms import EquityDocumentImportForm, EquityPositionForm
from .models import EquityPosition
from .services import (
    EquityDocumentImportError,
    build_equity_history_cards,
    extract_equity_position_prefill,
    sync_all_equities_market_data,
)


class EquityPositionListView(LoginRequiredMixin, TemplateView):
    template_name = "equities/equityposition_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        positions = list(EquityPosition.objects.prefetch_related("price_history"))
        context["page_title"] = "Acciones cotizadas"
        context["positions"] = positions
        context["summary"] = {
            "invested_amount": sum((position.invested_amount for position in positions), Decimal("0")),
            "current_value": sum((position.current_value for position in positions), Decimal("0")),
            "annual_income": sum((position.annual_dividend_income for position in positions), Decimal("0")),
            "synced_positions": sum((1 for position in positions if position.last_synced_at), 0),
        }
        context["history_cards"] = build_equity_history_cards(positions)
        context["position_form"] = kwargs.get("position_form", EquityPositionForm())
        context["document_form"] = kwargs.get("document_form", EquityDocumentImportForm())
        context["prefill_source_filename"] = kwargs.get("prefill_source_filename")
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

    def _save_position(self, request):
        form = EquityPositionForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(position_form=form)
            return self.render_to_response(context, status=400)

        ticker = form.cleaned_data["ticker"]
        broker = form.cleaned_data["broker"]
        ownership_category = form.cleaned_data["ownership_category"]
        position, created = EquityPosition.objects.update_or_create(
            broker=broker,
            ticker=ticker,
            ownership_category=ownership_category,
            defaults={
                "company_name": form.cleaned_data["company_name"],
                "quote_symbol": form.cleaned_data["quote_symbol"],
                "benchmark_symbol": form.cleaned_data["benchmark_symbol"],
                "benchmark_name": form.cleaned_data["benchmark_name"],
                "shares": form.cleaned_data["shares"],
                "average_cost_per_share": form.cleaned_data["average_cost_per_share"],
                "current_price_per_share": form.cleaned_data["current_price_per_share"],
                "annual_dividend_income": form.cleaned_data["annual_dividend_income"],
                "notes": form.cleaned_data["notes"],
            },
        )

        if created:
            messages.success(request, f"Posicion {position.ticker} creada correctamente.")
        else:
            messages.success(request, f"Posicion {position.ticker} actualizada correctamente.")
        return redirect("equities:list")
