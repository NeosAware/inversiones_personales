from pathlib import Path

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from .models import VentureDocument, VentureOpportunity


class FlexibleDecimalField(forms.DecimalField):
    def to_python(self, value):
        if isinstance(value, str):
            text = value.strip().replace("\xa0", "").replace(" ", "")
            if "," in text:
                text = text.replace(".", "").replace(",", ".")
            value = text
        return super().to_python(value)


class VentureOpportunityForm(forms.ModelForm):
    ticket_min = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        label="Ticket minimo",
    )
    ticket_max = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        label="Ticket maximo",
    )
    estimated_valuation = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        label="Valoracion estimada",
    )
    annual_revenue = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        label="Facturacion anual",
    )
    ebitda = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        label="EBITDA",
    )
    cash_need = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        label="Necesidad de caja",
    )

    class Meta:
        model = VentureOpportunity
        fields = (
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
            "ticket_min",
            "ticket_max",
            "estimated_valuation",
            "annual_revenue",
            "ebitda",
            "cash_need",
            "neos_fit_score",
            "market_score",
            "team_score",
            "financial_score",
            "risk_control_score",
            "fit_summary",
            "growth_issue",
            "synergy_notes",
            "diligence_notes",
            "red_flags",
            "next_steps",
            "notes",
        )
        labels = {
            "company_name": "Empresa",
            "website": "Web",
            "sector": "Sector",
            "geography": "Zona",
            "stage": "Estadio",
            "status": "Estado",
            "strategic_fit": "Encaje Neos",
            "contact_name": "Contacto",
            "source": "Origen",
            "identified_on": "Fecha de entrada",
            "next_review_on": "Proxima revision",
            "neos_fit_score": "Encaje Neos",
            "market_score": "Mercado",
            "team_score": "Equipo",
            "financial_score": "Finanzas",
            "risk_control_score": "Control de riesgo",
            "fit_summary": "Tesis de encaje",
            "growth_issue": "Problema de crecimiento",
            "synergy_notes": "Sinergias con Ceramica/Additives",
            "diligence_notes": "Due diligence pendiente",
            "red_flags": "Riesgos y senales rojas",
            "next_steps": "Proximos pasos",
            "notes": "Notas",
        }
        widgets = {
            "identified_on": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "next_review_on": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "neos_fit_score": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "market_score": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "team_score": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "financial_score": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "risk_control_score": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "fit_summary": forms.Textarea(attrs={"rows": 3}),
            "growth_issue": forms.Textarea(attrs={"rows": 3}),
            "synergy_notes": forms.Textarea(attrs={"rows": 3}),
            "diligence_notes": forms.Textarea(attrs={"rows": 3}),
            "red_flags": forms.Textarea(attrs={"rows": 3}),
            "next_steps": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_company_name(self):
        return self.cleaned_data["company_name"].strip()

    def clean_website(self):
        return self.cleaned_data.get("website", "").strip()

    def clean_sector(self):
        return self.cleaned_data.get("sector", "").strip()

    def clean_geography(self):
        return self.cleaned_data.get("geography", "").strip()

    def clean_contact_name(self):
        return self.cleaned_data.get("contact_name", "").strip()

    def clean_source(self):
        return self.cleaned_data.get("source", "").strip()

    def clean(self):
        cleaned_data = super().clean()
        ticket_min = cleaned_data.get("ticket_min")
        ticket_max = cleaned_data.get("ticket_max")
        if ticket_min is not None and ticket_max is not None and ticket_max < ticket_min:
            self.add_error("ticket_max", "El ticket maximo no puede ser inferior al ticket minimo.")
        return cleaned_data


class VentureBalanceAnalysisForm(forms.Form):
    opportunity = forms.ModelChoiceField(
        queryset=VentureOpportunity.objects.none(),
        label="Empresa",
    )
    title = forms.CharField(
        max_length=180,
        required=False,
        label="Titulo del documento",
        help_text="Opcional. Si lo dejas vacio se generara con el ano fiscal o la fecha.",
    )
    fiscal_year = forms.IntegerField(
        required=False,
        min_value=1990,
        max_value=2100,
        label="Ano fiscal",
    )
    document_date = forms.DateField(
        required=False,
        label="Fecha del documento",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    file = forms.FileField(
        label="Balance PDF",
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,application/pdf"}),
    )
    use_ai = forms.BooleanField(
        required=False,
        initial=True,
        label="Usar analista IA si esta configurado",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["opportunity"].queryset = VentureOpportunity.objects.order_by("company_name")

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        suffix = Path(str(getattr(uploaded_file, "name", "") or "")).suffix.lower()
        if suffix != ".pdf":
            raise ValidationError("Solo se admiten balances en PDF.")
        max_file_bytes = max(int(getattr(settings, "VENTURE_DOCUMENT_MAX_FILE_BYTES", 12 * 1024 * 1024) or 0), 1024)
        if getattr(uploaded_file, "size", 0) and uploaded_file.size > max_file_bytes:
            max_file_mb = max_file_bytes / (1024 * 1024)
            raise ValidationError(f"El PDF supera el limite de {max_file_mb:.1f} MB.")
        return uploaded_file

    def build_document_title(self):
        title = str(self.cleaned_data.get("title") or "").strip()
        if title:
            return title
        fiscal_year = self.cleaned_data.get("fiscal_year")
        if fiscal_year:
            return f"Balance {fiscal_year}"
        document_date = self.cleaned_data.get("document_date")
        if document_date:
            return f"Balance {document_date:%Y-%m-%d}"
        return "Balance PDF"

    def save_document(self):
        return VentureDocument.objects.create(
            opportunity=self.cleaned_data["opportunity"],
            document_kind=VentureDocument.DocumentKind.BALANCE,
            title=self.build_document_title(),
            fiscal_year=self.cleaned_data.get("fiscal_year"),
            document_date=self.cleaned_data.get("document_date"),
            file=self.cleaned_data["file"],
        )
