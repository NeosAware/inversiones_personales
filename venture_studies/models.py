from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class VentureOpportunity(models.Model):
    class Stage(models.TextChoices):
        EARLY = "early", "Estadio inicial"
        GROWTH_ISSUES = "growth_issues", "Problemas de crecimiento"
        TURNAROUND = "turnaround", "Reestructuracion"
        SCALEUP = "scaleup", "Escalado"
        OTHER = "other", "Otro"

    class Status(models.TextChoices):
        SCREENING = "screening", "Primer filtro"
        RESEARCH = "research", "En analisis"
        DUE_DILIGENCE = "due_diligence", "Due diligence"
        NEGOTIATION = "negotiation", "Negociacion"
        ON_HOLD = "on_hold", "En pausa"
        APPROVED = "approved", "Aprobada"
        REJECTED = "rejected", "Descartada"

    class StrategicFit(models.TextChoices):
        CERAMICA = "ceramica", "Neos Ceramica"
        ADDITIVES = "additives", "Neos Additives"
        BOTH = "both", "Ceramica + Additives"
        GROUP = "group", "Grupo Neos"
        OTHER = "other", "Otro encaje"

    company_name = models.CharField(max_length=180, unique=True)
    legal_name = models.CharField(max_length=180, blank=True)
    tax_id = models.CharField(max_length=24, blank=True, db_index=True)
    website = models.URLField(blank=True)
    sector = models.CharField(max_length=140, blank=True)
    geography = models.CharField(max_length=120, blank=True)
    address = models.CharField(max_length=240, blank=True)
    phone = models.CharField(max_length=60, blank=True)
    email = models.EmailField(blank=True)
    cnae_code = models.CharField(max_length=16, blank=True)
    cnae_label = models.CharField(max_length=180, blank=True)
    employees = models.PositiveIntegerField(null=True, blank=True)
    stage = models.CharField(max_length=24, choices=Stage.choices, default=Stage.EARLY)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.SCREENING)
    strategic_fit = models.CharField(max_length=24, choices=StrategicFit.choices, default=StrategicFit.BOTH)
    contact_name = models.CharField(max_length=140, blank=True)
    source = models.CharField(max_length=160, blank=True)
    identified_on = models.DateField(default=timezone.localdate)
    next_review_on = models.DateField(null=True, blank=True)

    ticket_min = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    ticket_max = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    estimated_valuation = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    annual_revenue = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    ebitda = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    cash_need = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    neos_fit_score = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    market_score = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    team_score = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    financial_score = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    risk_control_score = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )

    fit_summary = models.TextField(blank=True)
    growth_issue = models.TextField(blank=True)
    synergy_notes = models.TextField(blank=True)
    diligence_notes = models.TextField(blank=True)
    red_flags = models.TextField(blank=True)
    next_steps = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "-next_review_on", "-updated_at", "company_name"]

    def __str__(self):
        return self.company_name

    @property
    def score_total(self):
        return (
            self.neos_fit_score
            + self.market_score
            + self.team_score
            + self.financial_score
            + self.risk_control_score
        )

    @property
    def score_pct(self):
        return Decimal(self.score_total) / Decimal("25") * Decimal("100")

    @property
    def score_label(self):
        if self.status == self.Status.REJECTED:
            return "Descartada"
        if self.score_pct >= Decimal("80"):
            return "Prioridad alta"
        if self.score_pct >= Decimal("60"):
            return "Seguimiento"
        return "Observacion"

    @property
    def score_tone(self):
        if self.status == self.Status.REJECTED:
            return "warn"
        if self.score_pct >= Decimal("80"):
            return "good"
        if self.score_pct < Decimal("55"):
            return "warn"
        return ""

    @property
    def is_active(self):
        return self.status not in {self.Status.REJECTED, self.Status.ON_HOLD}


class VentureDocument(models.Model):
    class DocumentKind(models.TextChoices):
        BALANCE = "balance", "Balance o cuentas anuales"
        INFORMA = "informa", "Informe Informa"
        DOSSIER = "dossier", "Dossier financiero/comercial"
        PITCH = "pitch", "Presentacion"
        CONTRACT = "contract", "Contrato o pedido"
        OTHER = "other", "Otro documento"

    class ExtractionStatus(models.TextChoices):
        PENDING = "pending", "Pendiente"
        EXTRACTED = "extracted", "Texto extraido"
        FAILED = "failed", "Error de lectura"

    opportunity = models.ForeignKey(
        VentureOpportunity,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    document_kind = models.CharField(max_length=16, choices=DocumentKind.choices, default=DocumentKind.BALANCE)
    title = models.CharField(max_length=180)
    file = models.FileField(upload_to="venture_studies/%Y/%m")
    document_date = models.DateField(null=True, blank=True)
    fiscal_year = models.PositiveIntegerField(null=True, blank=True)
    extracted_text = models.TextField(blank=True)
    extraction_status = models.CharField(
        max_length=16,
        choices=ExtractionStatus.choices,
        default=ExtractionStatus.PENDING,
    )
    extraction_error = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "title"]

    def __str__(self):
        return f"{self.opportunity.company_name} - {self.title}"


class VentureAnalysisSnapshot(models.Model):
    class Recommendation(models.TextChoices):
        BUY = "buy", "Compra"
        WATCH = "watch", "Vigilancia"

    class Confidence(models.TextChoices):
        HIGH = "high", "Alta"
        MEDIUM = "medium", "Media"
        LOW = "low", "Baja"

    opportunity = models.ForeignKey(
        VentureOpportunity,
        on_delete=models.CASCADE,
        related_name="analysis_snapshots",
    )
    source_document = models.ForeignKey(
        VentureDocument,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_snapshots",
    )
    analysis_date = models.DateField(default=timezone.localdate)
    recommendation = models.CharField(
        max_length=12,
        choices=Recommendation.choices,
        default=Recommendation.WATCH,
    )
    confidence = models.CharField(
        max_length=12,
        choices=Confidence.choices,
        default=Confidence.MEDIUM,
    )
    score_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    valuation_low = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    valuation_base = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    valuation_high = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    suggested_purchase_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    suggested_ticket = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    target_ownership_pct = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    annual_revenue = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    ebitda = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    net_equity = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    net_debt = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    cash_need = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    summary = models.TextField(blank=True)
    valuation_note = models.TextField(blank=True)
    web_summary = models.TextField(blank=True)
    drivers = models.JSONField(default=list, blank=True)
    risks = models.JSONField(default=list, blank=True)
    assumptions = models.JSONField(default=list, blank=True)
    web_context = models.JSONField(default=dict, blank=True)
    analysis_payload = models.JSONField(default=dict, blank=True)
    agent_provider = models.CharField(max_length=32, default="core")
    agent_label = models.CharField(max_length=120, default="Analisis interno")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-analysis_date", "-created_at", "-id"]

    def __str__(self):
        return f"{self.opportunity.company_name} - {self.analysis_date} - {self.get_recommendation_display()}"

    @property
    def recommendation_tone(self):
        return "good" if self.recommendation == self.Recommendation.BUY else ""


class VentureDiscoveryCandidate(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Nueva"
        PROMOTED = "promoted", "Incorporada"
        REJECTED = "rejected", "Descartada"

    company_name = models.CharField(max_length=180)
    website = models.URLField(blank=True)
    sector = models.CharField(max_length=140, blank=True)
    geography = models.CharField(max_length=120, blank=True)
    source_title = models.CharField(max_length=240, blank=True)
    source_url = models.URLField(blank=True)
    source_label = models.CharField(max_length=120, blank=True)
    summary = models.TextField(blank=True)
    rationale = models.TextField(blank=True)
    score_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    tags = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NEW)
    promoted_opportunity = models.ForeignKey(
        VentureOpportunity,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="web_discovery_sources",
    )
    discovered_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "-score_pct", "-discovered_at", "company_name"]
        unique_together = ("company_name", "source_url")

    def __str__(self):
        return f"{self.company_name} ({self.get_status_display()})"

    @property
    def score_tone(self):
        if self.score_pct >= Decimal("75"):
            return "good"
        if self.score_pct < Decimal("50"):
            return "warn"
        return ""
