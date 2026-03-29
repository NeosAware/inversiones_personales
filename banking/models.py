from django.db import models

from portfolio.metrics import build_metrics


class BankBalance(models.Model):
    institution = models.CharField(max_length=120)
    account_name = models.CharField(max_length=120)
    deposited_amount = models.DecimalField(max_digits=14, decimal_places=2)
    current_balance = models.DecimalField(max_digits=14, decimal_places=2)
    annual_interest_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["institution", "account_name"]

    def __str__(self):
        return f"{self.institution} - {self.account_name}"

    def as_portfolio_position(self):
        return build_metrics(
            label=str(self),
            asset_type="Banking",
            invested_amount=self.deposited_amount,
            current_value=self.current_balance,
            annual_income=self.annual_interest_income,
            app_url_name="banking:list",
            notes=self.notes,
        )


class BankStatementImport(models.Model):
    class ImportStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        IMPORTED = "imported", "Imported"
        FAILED = "failed", "Failed"

    institution = models.CharField(max_length=120, blank=True)
    account_label = models.CharField(max_length=120, blank=True)
    iban = models.CharField(max_length=34, blank=True)
    currency = models.CharField(max_length=8, default="EUR")
    holder_name = models.CharField(max_length=180, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    source_file = models.FileField(upload_to="banking/statements/%Y/%m")
    source_filename = models.CharField(max_length=255)
    file_checksum = models.CharField(max_length=64, unique=True)
    import_status = models.CharField(
        max_length=20,
        choices=ImportStatus.choices,
        default=ImportStatus.PENDING,
    )
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    closing_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_expenses = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_pension_contributions = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_dividends = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    error_message = models.TextField(blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-period_end", "-imported_at"]

    def __str__(self):
        period = self.period_end.strftime("%Y-%m") if self.period_end else "No period"
        return f"{self.account_name} - {period}"

    def delete(self, using=None, keep_parents=False):
        storage = self.source_file.storage if self.source_file else None
        file_name = self.source_file.name if self.source_file else None
        result = super().delete(using=using, keep_parents=keep_parents)
        if storage and file_name:
            storage.delete(file_name)
        return result

    @property
    def account_name(self):
        if self.account_label:
            return self.account_label
        if self.iban:
            return f"Cuenta {self.iban[-4:]}"
        return self.source_filename

    @property
    def month_label(self):
        if self.period_end:
            return self.period_end.strftime("%Y-%m")
        return "No month"


class BankMovement(models.Model):
    class MovementGroup(models.TextChoices):
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"
        PENSION = "pension", "Pension contribution"
        DIVIDEND = "dividend", "Stock dividend"

    statement_import = models.ForeignKey(
        BankStatementImport,
        on_delete=models.CASCADE,
        related_name="movements",
    )
    booking_date = models.DateField()
    value_date = models.DateField(null=True, blank=True)
    concept = models.CharField(max_length=255)
    normalized_concept = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    reference_1 = models.CharField(max_length=120, blank=True)
    reference_2 = models.CharField(max_length=120, blank=True)
    movement_group = models.CharField(max_length=16, choices=MovementGroup.choices)
    concept_bucket = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-booking_date", "-id"]

    def __str__(self):
        return f"{self.booking_date} | {self.concept} | {self.amount}"


class BankInvestmentPosition(models.Model):
    class ProductType(models.TextChoices):
        SAVINGS_PLAN = "savings_plan", "Savings plan"
        LIFE_SAVINGS = "life_savings", "Life savings"
        BROKERED_EQUITY = "brokered_equity", "Brokered equity"
        OTHER = "other", "Other"

    institution = models.CharField(max_length=120)
    product_name = models.CharField(max_length=180)
    product_type = models.CharField(max_length=24, choices=ProductType.choices, default=ProductType.OTHER)
    invested_amount_override = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    current_value = models.DecimalField(max_digits=14, decimal_places=2)
    units = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    price_date = models.DateField(null=True, blank=True)
    portfolio_weight_pct = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    annual_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["institution", "product_type", "product_name"]

    def __str__(self):
        return f"{self.institution} - {self.product_name}"

    @property
    def invested_amount(self):
        return self.invested_amount_override if self.invested_amount_override is not None else self.current_value

    def as_portfolio_position(self):
        return build_metrics(
            label=str(self),
            asset_type="Banking",
            invested_amount=self.invested_amount,
            current_value=self.current_value,
            annual_income=self.annual_income,
            app_url_name="banking:list",
            notes=self.notes,
        )
