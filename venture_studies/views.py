from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from portfolio.user_management import can_user_manage_financial_data

from .forms import VentureOpportunityForm
from .models import VentureOpportunity
from .services import build_venture_study_context


class VentureOpportunityListView(LoginRequiredMixin, TemplateView):
    template_name = "venture_studies/ventureopportunity_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        opportunities = list(VentureOpportunity.objects.prefetch_related("documents"))
        context["page_title"] = "Radar de empresas no cotizadas"
        context["opportunities"] = opportunities
        context["can_manage_finances"] = can_user_manage_financial_data(self.request.user)
        context.setdefault("form", VentureOpportunityForm())
        context.update(build_venture_study_context(opportunities))
        return context

    def post(self, request, *args, **kwargs):
        if not can_user_manage_financial_data(request.user):
            messages.error(request, "Solo un administrador puede modificar el radar de empresas no cotizadas.")
            return redirect("venture_studies:list")

        form = VentureOpportunityForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(form=form)
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
            messages.success(request, f"La empresa {opportunity.company_name} se ha incorporado al radar.")
        else:
            messages.success(request, f"La empresa {opportunity.company_name} se ha actualizado correctamente.")
        return redirect("venture_studies:list")
