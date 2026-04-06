from django.db import models

from portfolio.metrics import build_metrics
from portfolio.ownership import AssetOwnershipCategory


class PropertyInvestment(models.Model):
    ownership_category = models.CharField(
        max_length=12,
        choices=AssetOwnershipCategory.choices,
        default=AssetOwnershipCategory.JOINT,
    )
    property_name = models.CharField(max_length=150)
    city = models.CharField(max_length=120)
    invested_equity = models.DecimalField(max_digits=14, decimal_places=2)
    market_value = models.DecimalField(max_digits=14, decimal_places=2)
    mortgage_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    annual_rent_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    annual_expenses = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ownership_category", "property_name"]

    def __str__(self):
        return self.property_name

    @property
    def current_value(self):
        return self.market_value - self.mortgage_balance

    @property
    def annual_income(self):
        return self.annual_rent_income - self.annual_expenses

    def as_portfolio_position(self):
        return build_metrics(
            label=f"{self.property_name} ({self.city}) - {self.get_ownership_category_display()}",
            asset_type="Inmuebles",
            invested_amount=self.invested_equity,
            current_value=self.current_value,
            annual_income=self.annual_income,
            app_url_name="real_estate:list",
            notes=self.notes,
        )
