from django.contrib import admin

from .models import VentureAnalysisSnapshot, VentureDiscoveryCandidate, VentureDocument, VentureOpportunity


class VentureDocumentInline(admin.TabularInline):
    model = VentureDocument
    extra = 0
    fields = ("document_kind", "title", "fiscal_year", "document_date", "file", "extraction_status", "uploaded_at")
    readonly_fields = ("extraction_status", "uploaded_at")


class VentureAnalysisSnapshotInline(admin.TabularInline):
    model = VentureAnalysisSnapshot
    extra = 0
    fields = (
        "analysis_date",
        "recommendation",
        "confidence",
        "score_pct",
        "suggested_purchase_price",
        "suggested_ticket",
        "agent_label",
        "created_at",
    )
    readonly_fields = ("created_at",)
    show_change_link = True


@admin.register(VentureOpportunity)
class VentureOpportunityAdmin(admin.ModelAdmin):
    list_display = (
        "company_name",
        "tax_id",
        "status",
        "stage",
        "strategic_fit",
        "score_pct_display",
        "ticket_min",
        "ticket_max",
        "latest_recommendation",
        "next_review_on",
        "updated_at",
    )
    list_filter = ("status", "stage", "strategic_fit", "cnae_code")
    search_fields = (
        "company_name",
        "legal_name",
        "tax_id",
        "sector",
        "cnae_label",
        "geography",
        "fit_summary",
        "synergy_notes",
        "red_flags",
    )
    readonly_fields = ("score_total", "score_pct", "updated_at")
    inlines = [VentureDocumentInline, VentureAnalysisSnapshotInline]
    fieldsets = (
        (
            "Identificacion",
            {
                "fields": (
                    "company_name",
                    "legal_name",
                    "tax_id",
                    "website",
                    "sector",
                    "geography",
                    "address",
                    "phone",
                    "email",
                    "cnae_code",
                    "cnae_label",
                    "employees",
                    "stage",
                    "status",
                    "strategic_fit",
                    "contact_name",
                    "source",
                    "identified_on",
                    "next_review_on",
                )
            },
        ),
        (
            "Economia de la oportunidad",
            {
                "fields": (
                    "ticket_min",
                    "ticket_max",
                    "estimated_valuation",
                    "annual_revenue",
                    "ebitda",
                    "cash_need",
                )
            },
        ),
        (
            "Scoring",
            {
                "fields": (
                    "neos_fit_score",
                    "market_score",
                    "team_score",
                    "financial_score",
                    "risk_control_score",
                    "score_total",
                    "score_pct",
                )
            },
        ),
        (
            "Analisis",
            {
                "fields": (
                    "fit_summary",
                    "growth_issue",
                    "synergy_notes",
                    "diligence_notes",
                    "red_flags",
                    "next_steps",
                    "notes",
                    "updated_at",
                )
            },
        ),
    )

    @admin.display(description="Score")
    def score_pct_display(self, obj):
        return f"{obj.score_pct:.0f} %"

    @admin.display(description="Ultimo analisis")
    def latest_recommendation(self, obj):
        latest = obj.analysis_snapshots.order_by("-analysis_date", "-created_at", "-id").first()
        if not latest:
            return "-"
        return f"{latest.get_recommendation_display()} ({latest.suggested_purchase_price or 0:.0f} EUR)"


@admin.register(VentureDocument)
class VentureDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "opportunity", "document_kind", "fiscal_year", "extraction_status", "uploaded_at")
    search_fields = ("title", "opportunity__company_name", "notes")
    list_filter = ("document_kind", "extraction_status", "uploaded_at")
    readonly_fields = ("extracted_text", "extraction_status", "extraction_error", "uploaded_at")


@admin.register(VentureAnalysisSnapshot)
class VentureAnalysisSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "opportunity",
        "analysis_date",
        "recommendation",
        "confidence",
        "score_pct",
        "suggested_purchase_price",
        "suggested_ticket",
        "agent_label",
        "created_at",
    )
    search_fields = ("opportunity__company_name", "summary", "valuation_note", "web_summary")
    list_filter = ("recommendation", "confidence", "agent_provider", "analysis_date")
    readonly_fields = ("created_at",)


@admin.register(VentureDiscoveryCandidate)
class VentureDiscoveryCandidateAdmin(admin.ModelAdmin):
    list_display = (
        "company_name",
        "status",
        "geography",
        "sector",
        "score_pct",
        "source_label",
        "promoted_opportunity",
        "discovered_at",
    )
    search_fields = ("company_name", "sector", "geography", "source_title", "summary", "rationale")
    list_filter = ("status", "geography", "sector", "discovered_at")
    readonly_fields = ("discovered_at", "updated_at")
