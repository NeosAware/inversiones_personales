from django.contrib import admin

from .models import BankBalance, BankConnection, BankExternalAccount, BankInvestmentPosition, BankMovement, BankStatementImport


@admin.register(BankBalance)
class BankBalanceAdmin(admin.ModelAdmin):
    list_display = (
        "institution",
        "account_name",
        "ownership_category",
        "deposited_amount",
        "current_balance",
        "annual_interest_income",
        "updated_at",
    )
    search_fields = ("institution", "account_name")
    list_filter = ("ownership_category", "institution")


class BankMovementInline(admin.TabularInline):
    model = BankMovement
    extra = 0
    fields = ("booking_date", "concept", "amount", "movement_group", "concept_bucket")
    readonly_fields = fields
    can_delete = False


@admin.register(BankStatementImport)
class BankStatementImportAdmin(admin.ModelAdmin):
    list_display = (
        "month_label",
        "account_name",
        "import_source",
        "ownership_category",
        "import_status",
        "total_income",
        "total_expenses",
        "total_pension_contributions",
        "total_dividends",
        "imported_at",
    )
    search_fields = ("source_filename", "iban", "holder_name", "account_label")
    list_filter = ("import_source", "statement_kind", "ownership_category", "import_status", "currency", "period_end")
    readonly_fields = (
        "file_checksum",
        "import_status",
        "error_message",
        "imported_at",
        "processed_at",
        "opening_balance",
        "closing_balance",
        "total_income",
        "total_expenses",
        "total_pension_contributions",
        "total_dividends",
    )
    inlines = [BankMovementInline]


@admin.register(BankMovement)
class BankMovementAdmin(admin.ModelAdmin):
    list_display = ("booking_date", "concept", "amount", "movement_group", "concept_bucket", "statement_import")
    search_fields = ("concept", "concept_bucket", "reference_1", "reference_2")
    list_filter = ("movement_group", "concept_bucket", "statement_import__period_end")


@admin.register(BankInvestmentPosition)
class BankInvestmentPositionAdmin(admin.ModelAdmin):
    list_display = (
        "institution",
        "product_name",
        "ownership_category",
        "product_type",
        "invested_amount_override",
        "current_value",
        "portfolio_weight_pct",
        "price_date",
        "updated_at",
    )
    search_fields = ("institution", "product_name", "notes")
    list_filter = ("ownership_category", "institution", "product_type")


@admin.register(BankConnection)
class BankConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "institution_name",
        "provider",
        "ownership_category",
        "country_code",
        "requisition_status",
        "active",
        "last_synced_at",
    )
    search_fields = ("institution_name", "institution_id", "reference", "requisition_id")
    list_filter = ("provider", "ownership_category", "country_code", "active", "requisition_status")
    readonly_fields = ("reference", "agreement_id", "requisition_id", "requisition_link", "last_synced_at", "last_error")


@admin.register(BankExternalAccount)
class BankExternalAccountAdmin(admin.ModelAdmin):
    list_display = (
        "account_label",
        "institution",
        "statement_kind",
        "ownership_category",
        "is_active",
        "last_imported_at",
    )
    search_fields = ("account_label", "iban", "holder_name", "provider_account_id")
    list_filter = ("statement_kind", "ownership_category", "institution", "is_active")
