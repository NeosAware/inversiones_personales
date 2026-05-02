from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from .ownership import AssetOwnershipCategory


class PortfolioSnapshot(models.Model):
    snapshot_date = models.DateField(unique=True)
    invested_amount = models.DecimalField(max_digits=14, decimal_places=2)
    current_value = models.DecimalField(max_digits=14, decimal_places=2)
    annual_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_return_eur = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_return_pct = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    section_values = models.JSONField(default=dict, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["snapshot_date"]

    def __str__(self):
        return f"Portfolio snapshot {self.snapshot_date}"


class HouseholdAlertSettings(models.Model):
    name = models.CharField(max_length=50, unique=True, default="default")
    total_monthly_expense_limit = models.DecimalField(max_digits=14, decimal_places=2, default=8000)
    concept_monthly_expense_limit = models.DecimalField(max_digits=14, decimal_places=2, default=1500)
    expense_spike_threshold_pct = models.DecimalField(max_digits=7, decimal_places=2, default=25)
    lookback_months = models.PositiveSmallIntegerField(default=3)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Household alert settings"

    def __str__(self):
        return self.name


class PlannedInvestmentPayment(models.Model):
    class FlowType(models.TextChoices):
        OUTFLOW = "outflow", "Pago previsto"
        INFLOW = "inflow", "Cobro previsto"

    class InvestmentBlock(models.TextChoices):
        EQUITIES = "equities", "Acciones cotizadas"
        UNLISTED = "unlisted", "Empresa no cotizada"
        REAL_ESTATE = "real_estate", "Inmuebles"
        BANKING_PRODUCT = "banking_product", "Producto bancario"
        NEOS_GROUP = "neos_group", "Grupo Neos"
        OTHER = "other", "Otro"

    class Status(models.TextChoices):
        PLANNED = "planned", "Previsto"
        PAID = "paid", "Realizado"
        CANCELLED = "cancelled", "Cancelado"

    ownership_category = models.CharField(
        max_length=12,
        choices=AssetOwnershipCategory.choices,
        default=AssetOwnershipCategory.JOINT,
    )
    title = models.CharField(max_length=180)
    investment_block = models.CharField(
        max_length=24,
        choices=InvestmentBlock.choices,
        default=InvestmentBlock.EQUITIES,
    )
    flow_type = models.CharField(max_length=12, choices=FlowType.choices, default=FlowType.OUTFLOW)
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PLANNED)
    paid_date = models.DateField(null=True, blank=True)
    paid_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "due_date", "title"]

    def __str__(self):
        return f"{self.title} - {self.due_date:%Y-%m-%d}"

    @property
    def signed_amount(self):
        sign = -1 if self.flow_type == self.FlowType.OUTFLOW else 1
        return self.amount * sign

    @property
    def effective_amount(self):
        return self.paid_amount if self.paid_amount is not None else self.amount

    @property
    def signed_effective_amount(self):
        sign = -1 if self.flow_type == self.FlowType.OUTFLOW else 1
        return self.effective_amount * sign


class SalesForecastSnapshot(models.Model):
    month = models.DateField(unique=True)
    forecast_revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    forecast_purchase_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    forecast_units = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    forecast_average_purchase_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    forecast_average_sale_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    actual_revenue = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    actual_purchase_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    actual_units = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    actual_average_purchase_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    actual_average_sale_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    source_label = models.CharField(max_length=120, blank=True, default="sales")
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["month"]

    def __str__(self):
        return f"Sales forecast {self.month:%Y-%m}"

    @property
    def forecast_margin(self):
        return self.forecast_revenue - self.forecast_purchase_cost

    @property
    def actual_margin(self):
        if self.actual_revenue is None and self.actual_purchase_cost is None:
            return None
        return (self.actual_revenue or Decimal("0.00")) - (self.actual_purchase_cost or Decimal("0.00"))

    @property
    def forecast_margin_pct(self):
        if not self.forecast_revenue:
            return None
        return (self.forecast_margin / self.forecast_revenue) * Decimal("100")

    @property
    def actual_margin_pct(self):
        if not self.actual_revenue:
            return None
        actual_margin = self.actual_margin
        if actual_margin is None:
            return None
        return (actual_margin / self.actual_revenue) * Decimal("100")
