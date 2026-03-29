from django.contrib import admin

from .models import PropertyInvestment


@admin.register(PropertyInvestment)
class PropertyInvestmentAdmin(admin.ModelAdmin):
    list_display = ("property_name", "city", "invested_equity", "market_value", "mortgage_balance", "annual_rent_income", "annual_expenses", "updated_at")
    search_fields = ("property_name", "city")
