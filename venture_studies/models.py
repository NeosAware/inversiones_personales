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
    website = models.URLField(blank=True)
    sector = models.CharField(max_length=140, blank=True)
    geography = models.CharField(max_length=120, blank=True)
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
    opportunity = models.ForeignKey(
        VentureOpportunity,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    title = models.CharField(max_length=180)
    file = models.FileField(upload_to="venture_studies/%Y/%m")
    notes = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "title"]

    def __str__(self):
        return f"{self.opportunity.company_name} - {self.title}"
