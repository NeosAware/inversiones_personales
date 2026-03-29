from django.contrib import admin

from .models import BankBalance, BankInvestmentPosition, BankMovement, BankStatementImport


@admin.register(BankBalance)
class BankBalanceAdmin(admin.ModelAdmin):
    list_display = ("institution", "account_name", "deposited_amount", "current_balance", "annual_interest_income", "updated_at")
    search_fields = ("institution", "account_name")


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
        "import_status",
        "total_income",
        "total_expenses",
        "total_pension_contributions",
        "total_dividends",
        "imported_at",
    )
    search_fields = ("source_filename", "iban", "holder_name", "account_label")
    list_filter = ("import_status", "currency", "period_end")
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
        "product_type",
        "invested_amount_override",
        "current_value",
        "portfolio_weight_pct",
        "price_date",
        "updated_at",
    )
    search_fields = ("institution", "product_name", "notes")
    list_filter = ("institution", "product_type")
