from decimal import Decimal

from django.conf import settings
from django.db import models

from portfolio.metrics import build_metrics
from portfolio.ownership import AssetOwnershipCategory

from .broker_costs import estimate_broker_costs, resolve_recurring_cost_used


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

    class TradeChannel(models.TextChoices):
        APP = "app", "App"
        WEB = "web", "Web"
        OFFICE = "office", "Oficina"
        CONTACT_CENTER = "contact_center", "Contact Center"
        OTHER = "other", "Otro"

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
    trade_channel = models.CharField(
        max_length=24,
        choices=TradeChannel.choices,
        default=TradeChannel.APP,
    )
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
    def estimated_broker_costs(self):
        return estimate_broker_costs(
            broker_name=self.broker,
            trade_channel=self.trade_channel,
            trade_amount=self.invested_amount,
            valuation_amount=self.current_value or self.invested_amount,
            annual_dividend_income=self.annual_dividend_income,
            quote_symbol=self.quote_symbol,
        )

    @property
    def recurring_cost_used(self):
        return resolve_recurring_cost_used(
            self.annual_maintenance_cost,
            self.estimated_broker_costs.get("annual_recurring_cost", Decimal("0.00")),
        )[0]

    @property
    def recurring_cost_source(self):
        return resolve_recurring_cost_used(
            self.annual_maintenance_cost,
            self.estimated_broker_costs.get("annual_recurring_cost", Decimal("0.00")),
        )[1]

    @property
    def purchase_total_cost(self):
        return self.estimated_broker_costs.get("purchase_total_cost", Decimal("0.00"))

    @property
    def net_dividend_income(self):
        return self.annual_dividend_income - self.estimated_broker_costs.get("annual_dividend_fee", Decimal("0.00"))

    @property
    def net_annual_income(self):
        return self.net_dividend_income - self.recurring_cost_used

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
        return self.unrealized_gain - self.purchase_total_cost - self.recurring_cost_used

    @property
    def unrealized_return_pct(self):
        committed_capital = self.invested_amount + self.purchase_total_cost
        if not committed_capital:
            return 0
        return (self.unrealized_gain_after_costs / committed_capital) * 100

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


class EquityTicketSnapshot(models.Model):
    position = models.ForeignKey(EquityPosition, on_delete=models.CASCADE, related_name="ticket_snapshots")
    snapshot_date = models.DateField()
    invested_amount = models.DecimalField(max_digits=14, decimal_places=2)
    current_value = models.DecimalField(max_digits=14, decimal_places=2)
    projected_market_value_12m = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    projected_total_value_12m = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    projected_price_12m = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["snapshot_date", "position_id"]
        unique_together = ("position", "snapshot_date")

    def __str__(self):
        return f"{self.position.ticker} snapshot {self.snapshot_date}"


class EquityOptimizationRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        RUNNING = "running", "En proceso"
        COMPLETED = "completed", "Completada"
        FAILED = "failed", "Fallida"

    reference_code = models.CharField(max_length=40, unique=True)
    label = models.CharField(max_length=160, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="equity_optimization_runs",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    status_note = models.CharField(max_length=255, blank=True)
    total_investment = models.DecimalField(max_digits=14, decimal_places=2)
    max_company_pct = models.DecimalField(max_digits=5, decimal_places=2)
    max_sector_positions = models.PositiveIntegerField(default=0)
    restrictions_note = models.TextField(blank=True)
    progress_data = models.JSONField(default=dict, blank=True)
    summary_data = models.JSONField(default=dict, blank=True)
    allocations_data = models.JSONField(default=list, blank=True)
    report_html = models.TextField(blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.reference_code

    @property
    def display_label(self):
        return self.label or self.reference_code
