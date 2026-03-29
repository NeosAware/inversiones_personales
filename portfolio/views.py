from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from .services import build_portfolio_dashboard, capture_portfolio_snapshot


class PortfolioDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "portfolio/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(build_portfolio_dashboard())
        return context

    def post(self, request, *args, **kwargs):
        capture_portfolio_snapshot()
        messages.success(request, "Portfolio snapshot captured for today.")
        return redirect("portfolio:dashboard")
