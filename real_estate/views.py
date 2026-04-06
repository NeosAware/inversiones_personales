from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from .forms import PropertyInvestmentForm
from .models import PropertyInvestment
from .services import build_property_ownership_overview


class PropertyInvestmentListView(LoginRequiredMixin, TemplateView):
    template_name = "real_estate/propertyinvestment_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        properties = list(PropertyInvestment.objects.all())
        ownership_overview = build_property_ownership_overview(properties)
        context["page_title"] = "Inmuebles"
        context["properties"] = properties
        context["summary"] = {
            "invested_amount": sum((property_item.invested_equity for property_item in properties), Decimal("0")),
            "current_value": sum((property_item.current_value for property_item in properties), Decimal("0")),
            "annual_income": sum((property_item.annual_income for property_item in properties), Decimal("0")),
        }
        context["ownership_overview"] = ownership_overview
        context.setdefault("form", PropertyInvestmentForm())
        return context

    def post(self, request, *args, **kwargs):
        form = PropertyInvestmentForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(form=form)
            return self.render_to_response(context, status=400)

        property_item, created = PropertyInvestment.objects.update_or_create(
            property_name=form.cleaned_data["property_name"],
            city=form.cleaned_data["city"],
            defaults={
                "ownership_category": form.cleaned_data["ownership_category"],
                "invested_equity": form.cleaned_data["invested_equity"],
                "market_value": form.cleaned_data["market_value"],
                "mortgage_balance": form.cleaned_data["mortgage_balance"],
                "annual_rent_income": form.cleaned_data["annual_rent_income"],
                "annual_expenses": form.cleaned_data["annual_expenses"],
                "notes": form.cleaned_data["notes"],
            },
        )
        if created:
            messages.success(request, f"El inmueble {property_item.property_name} se ha creado correctamente.")
        else:
            messages.success(request, f"El inmueble {property_item.property_name} se ha actualizado correctamente.")
        return redirect("real_estate:list")
