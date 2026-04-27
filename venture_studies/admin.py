from django.contrib import admin

from .models import VentureDocument, VentureOpportunity


class VentureDocumentInline(admin.TabularInline):
    model = VentureDocument
    extra = 0
    fields = ("title", "file", "notes", "uploaded_at")
    readonly_fields = ("uploaded_at",)


@admin.register(VentureOpportunity)
class VentureOpportunityAdmin(admin.ModelAdmin):
    list_display = (
        "company_name",
        "status",
        "stage",
        "strategic_fit",
        "score_pct_display",
        "ticket_min",
        "ticket_max",
        "next_review_on",
        "updated_at",
    )
    list_filter = ("status", "stage", "strategic_fit")
    search_fields = ("company_name", "sector", "geography", "fit_summary", "synergy_notes", "red_flags")
    readonly_fields = ("score_total", "score_pct", "updated_at")
    inlines = [VentureDocumentInline]
    fieldsets = (
        (
            "Identificacion",
            {
                "fields": (
                    "company_name",
                    "website",
                    "sector",
                    "geography",
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


@admin.register(VentureDocument)
class VentureDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "opportunity", "uploaded_at")
    search_fields = ("title", "opportunity__company_name", "notes")
    list_filter = ("uploaded_at",)
