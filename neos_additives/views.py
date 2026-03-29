from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from portfolio.company_valuation import build_company_valuation_context, save_annual_valuation
from portfolio.company_valuation_forms import AnnualCompanyValuationForm

from .models import AdditivesAnnualValuation, AdditivesHolding


AUTO_HOLDING_NAME = "Neos Additives fiscal valuation stake"


class AdditivesHoldingListView(LoginRequiredMixin, TemplateView):
    template_name = "company/company_dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Neos Additives"
        context["module_blurb"] = (
            "Upload annual balance, P&L or corporate tax PDFs to calculate the AEAT tax value of the company and your stake."
        )
        context["annual_form"] = kwargs.get("annual_form", AnnualCompanyValuationForm())
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
        messages.success(request, f"Neos Additives {record.year} valuation recalculated successfully.")
        return redirect("neos_additives:list")
