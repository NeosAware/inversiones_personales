from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView

from .models import PropertyInvestment


class PropertyInvestmentListView(LoginRequiredMixin, ListView):
    model = PropertyInvestment
    template_name = "real_estate/propertyinvestment_list.html"
    context_object_name = "properties"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        properties = list(context["properties"])
        context["page_title"] = "Inmuebles"
        context["summary"] = {
            "invested_amount": sum((property_item.invested_equity for property_item in properties), Decimal("0")),
            "current_value": sum((property_item.current_value for property_item in properties), Decimal("0")),
            "annual_income": sum((property_item.annual_income for property_item in properties), Decimal("0")),
        }
        return context
