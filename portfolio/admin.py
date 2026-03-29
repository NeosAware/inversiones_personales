from django.contrib import admin

from .models import HouseholdAlertSettings, PortfolioSnapshot


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
