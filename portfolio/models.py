from django.db import models


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
