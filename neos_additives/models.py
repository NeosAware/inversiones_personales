from django.db import models

from portfolio.metrics import build_metrics


class AdditivesHolding(models.Model):
    investment_name = models.CharField(max_length=150)
    invested_amount = models.DecimalField(max_digits=14, decimal_places=2)
    current_valuation = models.DecimalField(max_digits=14, decimal_places=2)
    annual_dividend_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["investment_name"]

    def __str__(self):
        return self.investment_name

    def as_portfolio_position(self):
        return build_metrics(
            label=self.investment_name,
            asset_type="Neos Additives",
            invested_amount=self.invested_amount,
            current_value=self.current_valuation,
            annual_income=self.annual_dividend_income,
            app_url_name="neos_additives:list",
            notes=self.notes,
        )


class AdditivesAnnualValuation(models.Model):
    class ValuationMethod(models.TextChoices):
        AUDITED_BALANCE = "audited_balance", "Balance aprobado auditado"
        THEORETICAL_VALUE = "theoretical_value", "Valor teorico contable"
        EARNINGS_CAPITALISATION = "earnings_capitalisation", "Capitalizacion del beneficio a 3 anos"
        NOMINAL_VALUE = "nominal_value", "Capital social nominal"

    year = models.PositiveIntegerField(unique=True)
    ownership_pct = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    balance_approved = models.BooleanField(default=False)
    audited_favorable = models.BooleanField(default=False)
    balance_pdf = models.FileField(upload_to="neos_additives/annual/%Y", blank=True)
    profit_loss_pdf = models.FileField(upload_to="neos_additives/annual/%Y", blank=True)
    corporate_tax_pdf = models.FileField(upload_to="neos_additives/annual/%Y", blank=True)
    net_equity = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    share_capital = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    profit_after_tax = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    three_year_average_profit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    capitalised_earnings_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    tax_company_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    owner_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    valuation_method = models.CharField(max_length=32, choices=ValuationMethod.choices, blank=True)
    calculation_note = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year"]

    def __str__(self):
        return f"Neos Additives {self.year}"
