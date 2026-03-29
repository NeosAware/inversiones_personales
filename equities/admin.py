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
        "shares",
        "average_cost_per_share",
        "current_price_per_share",
        "latest_price_date",
        "last_synced_at",
    )
    search_fields = ("ticker", "quote_symbol", "company_name", "broker")


@admin.register(EquityPriceHistory)
class EquityPriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("position", "price_date", "close_price", "benchmark_close")
    search_fields = ("position__ticker", "position__company_name")
    list_filter = ("position__benchmark_symbol",)
