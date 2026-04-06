from django.db import models

from portfolio.metrics import build_metrics
from portfolio.ownership import AssetOwnershipCategory


class BankBalance(models.Model):
    ownership_category = models.CharField(
        max_length=12,
        choices=AssetOwnershipCategory.choices,
        default=AssetOwnershipCategory.JOINT,
    )
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
            label=f"{self} ({self.get_ownership_category_display()})",
            asset_type="Banca",
            invested_amount=self.deposited_amount,
            current_value=self.current_balance,
            annual_income=self.annual_interest_income,
            app_url_name="banking:list",
            notes=self.notes,
        )


class BankStatementImport(models.Model):
    class ImportSource(models.TextChoices):
        UPLOAD = "upload", "Subida manual"
        OPEN_BANKING = "open_banking", "Open banking"
        ROBOT = "robot", "Robot local"

    class StatementKind(models.TextChoices):
        ACCOUNT = "account", "Cuenta"
        CARD = "card", "Tarjeta"

    class ImportStatus(models.TextChoices):
        PENDING = "pending", "Pendiente"
        IMPORTED = "imported", "Importado"
        FAILED = "failed", "Fallido"

    ownership_category = models.CharField(
        max_length=12,
        choices=AssetOwnershipCategory.choices,
        default=AssetOwnershipCategory.JOINT,
    )
    connection = models.ForeignKey(
        "BankConnection",
        on_delete=models.SET_NULL,
        related_name="statement_imports",
        null=True,
        blank=True,
    )
    external_account = models.ForeignKey(
        "BankExternalAccount",
        on_delete=models.SET_NULL,
        related_name="statement_imports",
        null=True,
        blank=True,
    )
    import_source = models.CharField(
        max_length=20,
        choices=ImportSource.choices,
        default=ImportSource.UPLOAD,
    )
    institution = models.CharField(max_length=120, blank=True)
    account_label = models.CharField(max_length=120, blank=True)
    iban = models.CharField(max_length=34, blank=True)
    statement_kind = models.CharField(
        max_length=16,
        choices=StatementKind.choices,
        default=StatementKind.ACCOUNT,
    )
    currency = models.CharField(max_length=8, default="EUR")
    holder_name = models.CharField(max_length=180, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    source_file = models.FileField(upload_to="banking/statements/%Y/%m", blank=True)
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
        period = self.period_label
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
        return "Sin mes"

    @property
    def period_label(self):
        if self.period_start and self.period_end:
            start_label = self.period_start.strftime("%Y-%m-%d")
            end_label = self.period_end.strftime("%Y-%m-%d")
            return start_label if start_label == end_label else f"{start_label} a {end_label}"
        if self.period_end:
            return f"Hasta {self.period_end:%Y-%m-%d}"
        if self.period_start:
            return f"Desde {self.period_start:%Y-%m-%d}"
        return "Sin periodo"


class BankConnection(models.Model):
    class Provider(models.TextChoices):
        GOCARDLESS = "gocardless", "GoCardless Open Banking"

    ownership_category = models.CharField(
        max_length=12,
        choices=AssetOwnershipCategory.choices,
        default=AssetOwnershipCategory.JOINT,
    )
    provider = models.CharField(max_length=32, choices=Provider.choices, default=Provider.GOCARDLESS)
    institution_name = models.CharField(max_length=160)
    institution_id = models.CharField(max_length=160)
    country_code = models.CharField(max_length=2, default="ES")
    reference = models.CharField(max_length=80, unique=True)
    agreement_id = models.CharField(max_length=64, blank=True)
    requisition_id = models.CharField(max_length=64, blank=True)
    requisition_link = models.URLField(blank=True)
    requisition_status = models.CharField(max_length=16, blank=True)
    consent_expires_at = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["institution_name", "created_at"]

    def __str__(self):
        return f"{self.institution_name} ({self.get_ownership_category_display()})"


class BankExternalAccount(models.Model):
    connection = models.ForeignKey(
        BankConnection,
        on_delete=models.CASCADE,
        related_name="external_accounts",
    )
    ownership_category = models.CharField(
        max_length=12,
        choices=AssetOwnershipCategory.choices,
        default=AssetOwnershipCategory.JOINT,
    )
    statement_kind = models.CharField(
        max_length=16,
        choices=BankStatementImport.StatementKind.choices,
        default=BankStatementImport.StatementKind.ACCOUNT,
    )
    provider_account_id = models.CharField(max_length=80, unique=True)
    institution = models.CharField(max_length=120, blank=True)
    account_label = models.CharField(max_length=180)
    iban = models.CharField(max_length=34, blank=True)
    currency = models.CharField(max_length=8, default="EUR")
    holder_name = models.CharField(max_length=180, blank=True)
    linked_account_name = models.CharField(max_length=180, blank=True)
    raw_details = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    last_imported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["institution", "account_label", "created_at"]

    def __str__(self):
        return f"{self.account_label} ({self.get_statement_kind_display()})"


class BankMovement(models.Model):
    class MovementGroup(models.TextChoices):
        INCOME = "income", "Ingreso"
        EXPENSE = "expense", "Gasto"
        PENSION = "pension", "Aportacion a plan"
        DIVIDEND = "dividend", "Dividendo"

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
        SAVINGS_PLAN = "savings_plan", "Plan de ahorro"
        LIFE_SAVINGS = "life_savings", "Ahorro vida"
        BROKERED_EQUITY = "brokered_equity", "Acciones en custodia"
        OTHER = "other", "Otro"

    ownership_category = models.CharField(
        max_length=12,
        choices=AssetOwnershipCategory.choices,
        default=AssetOwnershipCategory.JOINT,
    )
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
            label=f"{self} ({self.get_ownership_category_display()})",
            asset_type="Banca",
            invested_amount=self.invested_amount,
            current_value=self.current_value,
            annual_income=self.annual_income,
            app_url_name="banking:list",
            notes=self.notes,
        )
