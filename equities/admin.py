from django.contrib import admin

from .models import EquityPosition, EquityPriceHistory


@admin.register(EquityPosition)
class EquityPositionAdmin(admin.ModelAdmin):
    list_display = (
        "ticker",
        "quote_symbol",
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
    list_filter = ("ownership_category", "broker")


@admin.register(EquityPriceHistory)
class EquityPriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("position", "price_date", "open_price", "high_price", "low_price", "close_price", "benchmark_close")
    search_fields = ("position__ticker", "position__company_name")
    list_filter = ("position__benchmark_symbol",)
