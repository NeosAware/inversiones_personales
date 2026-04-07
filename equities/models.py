from django.db import models
from decimal import Decimal

from portfolio.metrics import build_metrics
from portfolio.ownership import AssetOwnershipCategory


class EquityPosition(models.Model):
    class PositionKind(models.TextChoices):
        OWNED = "owned", "Comprada"
        WATCHLIST = "watchlist", "En seguimiento"

    class ReferenceProfile(models.TextChoices):
        MARKET_INDEX = "market_index", "Indice o activo cotizado"
        EURIBOR_12M = "euribor_12m", "Euribor 12 meses"
        SPAIN_HOUSE_PRICE = "spain_house_price", "Precio vivienda Espana"
        SPAIN_ELECTRICITY_DEMAND = "spain_electricity_demand", "Demanda electrica Espana"
        SPAIN_GAS_CONSUMPTION = "spain_gas_consumption", "Consumo de gas Espana"

    position_kind = models.CharField(
        max_length=16,
        choices=PositionKind.choices,
        default=PositionKind.OWNED,
    )
    ownership_category = models.CharField(
        max_length=12,
        choices=AssetOwnershipCategory.choices,
        default=AssetOwnershipCategory.JOINT,
    )
    broker = models.CharField(max_length=120)
    ticker = models.CharField(max_length=20)
    quote_symbol = models.CharField(max_length=40, blank=True)
    reference_profile = models.CharField(
        max_length=24,
        choices=ReferenceProfile.choices,
        default=ReferenceProfile.MARKET_INDEX,
    )
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
        ordering = ["position_kind", "ticker"]

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
    def is_owned(self):
        return self.position_kind == self.PositionKind.OWNED

    @property
    def analysis_reference_label(self):
        return self.benchmark_name or self.get_reference_profile_display()

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
        invested_amount = self.invested_amount if self.is_owned else Decimal("0")
        current_value = self.current_value if self.is_owned else Decimal("0")
        annual_income = self.net_annual_income if self.is_owned else Decimal("0")
        return build_metrics(
            label=f"{self} ({self.get_ownership_category_display()})",
            asset_type="Acciones",
            invested_amount=invested_amount,
            current_value=current_value,
            annual_income=annual_income,
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
