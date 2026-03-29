from django.contrib import admin

from .models import MaterialsAnnualValuation, MaterialsHolding


@admin.register(MaterialsHolding)
class MaterialsHoldingAdmin(admin.ModelAdmin):
    list_display = ("investment_name", "invested_amount", "current_valuation", "annual_dividend_income", "updated_at")
    search_fields = ("investment_name",)


@admin.register(MaterialsAnnualValuation)
class MaterialsAnnualValuationAdmin(admin.ModelAdmin):
    list_display = (
        "year",
        "ownership_pct",
        "tax_company_value",
        "owner_value",
        "valuation_method",
        "balance_approved",
        "audited_favorable",
        "updated_at",
    )
    search_fields = ("year", "calculation_note")
    list_filter = ("balance_approved", "audited_favorable", "valuation_method")
