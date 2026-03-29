from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from .models import EquityPosition
from .services import build_equity_history_cards, sync_all_equities_market_data


class EquityPositionListView(LoginRequiredMixin, TemplateView):
    template_name = "equities/equityposition_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        positions = list(EquityPosition.objects.prefetch_related("price_history"))
        context["page_title"] = "Listed equities"
        context["positions"] = positions
        context["summary"] = {
            "invested_amount": sum((position.invested_amount for position in positions), Decimal("0")),
            "current_value": sum((position.current_value for position in positions), Decimal("0")),
            "annual_income": sum((position.annual_dividend_income for position in positions), Decimal("0")),
            "synced_positions": sum((1 for position in positions if position.last_synced_at), 0),
        }
        context["history_cards"] = build_equity_history_cards(positions)
        return context

    def post(self, request, *args, **kwargs):
        positions = list(EquityPosition.objects.all())
        results = sync_all_equities_market_data(positions)
        updated = sum(1 for _, error in results if error is None)
        failed = [f"{position.ticker}: {error}" for position, error in results if error]

        if updated:
            messages.success(request, f"Market data refreshed for {updated} position(s).")
        for failure in failed:
            messages.warning(request, failure)

        return redirect("equities:list")
