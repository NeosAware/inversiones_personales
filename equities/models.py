from django.db import models

from portfolio.metrics import build_metrics
from portfolio.ownership import AssetOwnershipCategory


class EquityPosition(models.Model):
    ownership_category = models.CharField(
        max_length=12,
        choices=AssetOwnershipCategory.choices,
        default=AssetOwnershipCategory.JOINT,
    )
    broker = models.CharField(max_length=120)
    ticker = models.CharField(max_length=20)
    quote_symbol = models.CharField(max_length=40, blank=True)
    benchmark_symbol = models.CharField(max_length=40, blank=True, default="^IBEX")
    benchmark_name = models.CharField(max_length=120, blank=True, default="IBEX 35")
    company_name = models.CharField(max_length=160)
    shares = models.DecimalField(max_digits=14, decimal_places=4)
    average_cost_per_share = models.DecimalField(max_digits=14, decimal_places=4)
    current_price_per_share = models.DecimalField(max_digits=14, decimal_places=4)
    annual_dividend_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    annual_maintenance_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    latest_price_date = models.DateField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ticker"]

    def __str__(self):
        return f"{self.ticker} - {self.company_name}"

    @property
    def invested_amount(self):
        return self.shares * self.average_cost_per_share

    @property
    def current_value(self):
        return self.shares * self.current_price_per_share

    @property
    def net_annual_income(self):
        return self.annual_dividend_income - self.annual_maintenance_cost

    @property
    def unrealized_gain(self):
        return self.current_value - self.invested_amount

    @property
    def unrealized_gain_after_costs(self):
        return self.unrealized_gain - self.annual_maintenance_cost

    @property
    def unrealized_return_pct(self):
        if not self.invested_amount:
            return 0
        return (self.unrealized_gain_after_costs / self.invested_amount) * 100

    def as_portfolio_position(self):
        return build_metrics(
            label=f"{self} ({self.get_ownership_category_display()})",
            asset_type="Acciones",
            invested_amount=self.invested_amount,
            current_value=self.current_value,
            annual_income=self.net_annual_income,
            app_url_name="equities:list",
            notes=self.notes,
        )


class EquityPriceHistory(models.Model):
    position = models.ForeignKey(EquityPosition, on_delete=models.CASCADE, related_name="price_history")
    price_date = models.DateField()
    open_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    high_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    low_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    close_price = models.DecimalField(max_digits=14, decimal_places=4)
    benchmark_close = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)

    class Meta:
        ordering = ["price_date"]
        unique_together = ("position", "price_date")

    def __str__(self):
        return f"{self.position.ticker} - {self.price_date}"
