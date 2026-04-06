from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from portfolio.company_group import NEOS_COMPANY_CONFIG
from portfolio.company_valuation import build_company_valuation_context, save_annual_valuation
from portfolio.company_valuation_forms import AnnualCompanyValuationForm

from .models import AdditivesAnnualValuation, AdditivesHolding


AUTO_HOLDING_NAME = "Neos Additives fiscal valuation stake"


class AdditivesHoldingListView(LoginRequiredMixin, TemplateView):
    template_name = "company/company_dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company_config = NEOS_COMPANY_CONFIG["Neos Additives"]
        context["page_title"] = "Neos Additives"
        context["module_blurb"] = (
            "Sube el balance anual, la cuenta de resultados o el impuesto de sociedades en PDF para calcular el valor fiscal AEAT de la empresa y de tu participacion."
        )
        context["group_role_title"] = company_config["role_title"]
        context["group_role_blurb"] = company_config["role_blurb"]
        context["annual_form"] = kwargs.get(
            "annual_form",
            AnnualCompanyValuationForm(initial={"ownership_pct": Decimal("80.00")}),
        )
        context.update(build_company_valuation_context(AdditivesHolding, AdditivesAnnualValuation))
        return context

    def post(self, request, *args, **kwargs):
        form = AnnualCompanyValuationForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self.get_context_data(annual_form=form)
            return self.render_to_response(context, status=400)

        record = save_annual_valuation(
            form.cleaned_data,
            AdditivesAnnualValuation,
            AdditivesHolding,
            AUTO_HOLDING_NAME,
        )
        messages.success(request, f"La valoracion de Neos Additives de {record.year} se ha recalculado correctamente.")
        return redirect("neos_additives:list")
