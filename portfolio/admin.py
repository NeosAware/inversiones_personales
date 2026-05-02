from django.contrib import admin

from .models import HouseholdAlertSettings, PlannedInvestmentPayment, PortfolioSnapshot, SalesForecastSnapshot


@admin.register(PortfolioSnapshot)
class PortfolioSnapshotAdmin(admin.ModelAdmin):
    list_display = ("snapshot_date", "invested_amount", "current_value", "annual_income", "total_return_pct")
    ordering = ("-snapshot_date",)


@admin.register(HouseholdAlertSettings)
class HouseholdAlertSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "total_monthly_expense_limit",
        "concept_monthly_expense_limit",
        "expense_spike_threshold_pct",
        "lookback_months",
        "active",
        "updated_at",
    )


@admin.register(PlannedInvestmentPayment)
class PlannedInvestmentPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "due_date",
        "title",
        "investment_block",
        "ownership_category",
        "flow_type",
        "amount",
        "status",
        "paid_date",
        "updated_at",
    )
    list_filter = ("status", "flow_type", "investment_block", "ownership_category", "due_date")
    search_fields = ("title", "notes")
    ordering = ("status", "due_date", "title")


@admin.register(SalesForecastSnapshot)
class SalesForecastSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "month",
        "forecast_revenue",
        "forecast_purchase_cost",
        "actual_revenue",
        "actual_purchase_cost",
        "source_label",
        "updated_at",
    )
    search_fields = ("source_label", "notes")
    ordering = ("-month",)
