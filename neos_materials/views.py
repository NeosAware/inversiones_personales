from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from portfolio.company_valuation import build_company_valuation_context, save_annual_valuation
from portfolio.company_valuation_forms import AnnualCompanyValuationForm

from .models import MaterialsAnnualValuation, MaterialsHolding


AUTO_HOLDING_NAME = "Neos Materials fiscal valuation stake"


class MaterialsHoldingListView(LoginRequiredMixin, TemplateView):
    template_name = "company/company_dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Neos Materials"
        context["module_blurb"] = (
            "Sube el balance anual, la cuenta de resultados o el impuesto de sociedades en PDF para calcular el valor fiscal AEAT de la empresa y de tu participacion."
        )
        context["annual_form"] = kwargs.get("annual_form", AnnualCompanyValuationForm())
        context.update(build_company_valuation_context(MaterialsHolding, MaterialsAnnualValuation))
        return context

    def post(self, request, *args, **kwargs):
        form = AnnualCompanyValuationForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self.get_context_data(annual_form=form)
            return self.render_to_response(context, status=400)

        record = save_annual_valuation(
            form.cleaned_data,
            MaterialsAnnualValuation,
            MaterialsHolding,
            AUTO_HOLDING_NAME,
        )
        messages.success(request, f"La valoracion de Neos Materials de {record.year} se ha recalculado correctamente.")
        return redirect("neos_materials:list")
