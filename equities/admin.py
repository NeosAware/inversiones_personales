from django.contrib import admin

from .models import (
    EquityNightlyAnalysisRun,
    EquityNightlyAnalysisSnapshot,
    EquityOptimizationRun,
    EquityPosition,
    EquityPriceHistory,
    EquityTicketSnapshot,
)


@admin.register(EquityPosition)
class EquityPositionAdmin(admin.ModelAdmin):
    list_display = (
        "position_kind",
        "ticker",
        "quote_symbol",
        "reference_profile",
        "benchmark_symbol",
        "company_name",
        "broker",
        "ownership_category",
        "shares",
        "average_cost_per_share",
        "current_price_per_share",
        "annual_maintenance_cost",
        "latest_price_date",
        "last_synced_at",
    )
    search_fields = ("ticker", "quote_symbol", "company_name", "broker")
    list_filter = ("position_kind", "reference_profile", "ownership_category", "broker")


@admin.register(EquityPriceHistory)
class EquityPriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("position", "price_date", "open_price", "high_price", "low_price", "close_price", "benchmark_close")
    search_fields = ("position__ticker", "position__company_name")
    list_filter = ("position__benchmark_symbol",)


@admin.register(EquityTicketSnapshot)
class EquityTicketSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "snapshot_date",
        "position",
        "invested_amount",
        "current_value",
        "projected_market_value_12m",
        "projected_total_value_12m",
    )
    search_fields = ("position__ticker", "position__company_name")
    list_filter = ("snapshot_date", "position__broker")


@admin.register(EquityOptimizationRun)
class EquityOptimizationRunAdmin(admin.ModelAdmin):
    list_display = (
        "reference_code",
        "label",
        "status",
        "created_at",
        "started_at",
        "completed_at",
        "total_investment",
        "max_company_pct",
        "max_total_positions",
        "max_sector_positions",
    )
    search_fields = ("reference_code", "label", "requested_by__username")
    list_filter = ("status", "created_at", "requested_by")


@admin.register(EquityNightlyAnalysisRun)
class EquityNightlyAnalysisRunAdmin(admin.ModelAdmin):
    list_display = (
        "analysis_date",
        "status",
        "agent_provider",
        "agent_label",
        "started_at",
        "completed_at",
    )
    search_fields = ("analysis_date", "agent_provider", "agent_label")
    list_filter = ("status", "agent_provider", "analysis_date")


@admin.register(EquityNightlyAnalysisSnapshot)
class EquityNightlyAnalysisSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "analysis_date",
        "scope",
        "ticker",
        "company_name",
        "status_key",
        "position",
        "agent_provider",
    )
    search_fields = ("ticker", "company_name", "quote_symbol", "analysis_key")
    list_filter = ("analysis_date", "scope", "status_key", "agent_provider")
