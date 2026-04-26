from pathlib import Path

from django.conf import settings
from django import forms
from django.core.exceptions import ValidationError

from portfolio.ownership import AssetOwnershipCategory

from .models import BankStatementImport


ROBOT_BANK_SUGGESTIONS = (
    "Banco Sabadell",
    "CaixaBank",
    "BBVA",
    "Banco Santander",
    "ING",
    "Abanca",
    "Kutxabank",
    "Unicaja",
    "EVO Banco",
    "Bankinter",
)

ALLOWED_STATEMENT_EXTENSIONS = {".xls", ".xlsx"}


def validate_statement_upload_batch(files):
    uploaded_files = [item for item in list(files or []) if item]
    if not uploaded_files:
        raise ValidationError("No se ha enviado ningun fichero.")

    max_files = max(int(getattr(settings, "BANK_STATEMENT_MAX_FILES_PER_REQUEST", 8) or 8), 1)
    if len(uploaded_files) > max_files:
        raise ValidationError(f"Solo puedes subir hasta {max_files} fichero(s) por importacion.")

    max_file_bytes = max(int(getattr(settings, "BANK_STATEMENT_MAX_FILE_BYTES", 8 * 1024 * 1024) or 0), 1024)
    max_file_mb = max_file_bytes / (1024 * 1024)
    for uploaded_file in uploaded_files:
        suffix = Path(str(getattr(uploaded_file, "name", "") or "")).suffix.lower()
        if suffix not in ALLOWED_STATEMENT_EXTENSIONS:
            raise ValidationError("Solo se admiten extractos XLS o XLSX.")
        file_size = getattr(uploaded_file, "size", None)
        if file_size is not None and int(file_size) > max_file_bytes:
            raise ValidationError(
                f"{uploaded_file.name} supera el limite de {max_file_mb:.1f} MB por fichero."
            )
    return uploaded_files


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class FlexibleDecimalField(forms.DecimalField):
    def to_python(self, value):
        if isinstance(value, str):
            text = value.strip().replace("\xa0", "").replace(" ", "")
            if "," in text:
                text = text.replace(".", "").replace(",", ".")
            value = text
        return super().to_python(value)


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            cleaned_files = [single_file_clean(item, initial) for item in data]
        else:
            cleaned_files = [single_file_clean(data, initial)]
        return validate_statement_upload_batch(cleaned_files)


class StatementUploadForm(forms.Form):
    statement_kind = forms.ChoiceField(
        choices=BankStatementImport.StatementKind.choices,
        initial=BankStatementImport.StatementKind.ACCOUNT,
        label="Tipo de documento",
        help_text="Elige Cuenta para extractos bancarios y Tarjeta para liquidaciones o movimientos exportados de la tarjeta.",
    )
    files = MultipleFileField(
        label="Documentos bancarios",
        widget=MultipleFileInput(attrs={"accept": ".xls,.xlsx"}),
        help_text="Sube uno o varios documentos bancarios en formato XLS o XLSX.",
    )


class BankBalanceForm(forms.Form):
    ownership_category = forms.ChoiceField(
        choices=AssetOwnershipCategory.choices,
        initial=AssetOwnershipCategory.JOINT,
        label="Titular",
    )
    institution = forms.CharField(max_length=120, label="Entidad")
    account_name = forms.CharField(max_length=120, label="Cuenta")
    deposited_amount = FlexibleDecimalField(max_digits=14, decimal_places=2, label="Capital depositado")
    current_balance = FlexibleDecimalField(max_digits=14, decimal_places=2, label="Saldo actual")
    annual_interest_income = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Interes anual",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Notas")

    def clean_institution(self):
        return self.cleaned_data["institution"].strip()

    def clean_account_name(self):
        return self.cleaned_data["account_name"].strip()

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("annual_interest_income") is None:
            cleaned_data["annual_interest_income"] = 0
        return cleaned_data


class RobotSetupAssistantForm(forms.Form):
    bank_name = forms.CharField(
        max_length=120,
        initial="Banco Sabadell",
        label="Banco",
        help_text="Escribe el nombre del banco tal como lo conoces.",
        widget=forms.TextInput(
            attrs={
                "list": "robot-bank-suggestions",
                "placeholder": "Ejemplo: Banco Sabadell",
            }
        ),
    )
    ownership_category = forms.ChoiceField(
        choices=AssetOwnershipCategory.choices,
        initial=AssetOwnershipCategory.JOINT,
        label="Titular",
    )
    statement_kind = forms.ChoiceField(
        choices=BankStatementImport.StatementKind.choices,
        initial=BankStatementImport.StatementKind.ACCOUNT,
        label="Que quieres descargar",
    )
    account_label = forms.CharField(
        max_length=120,
        required=False,
        label="Nombre corto",
        help_text="Opcional. Ejemplo: cuenta nomina, tarjeta visa, ahorro familiar.",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Ejemplo: cuenta nomina",
            }
        ),
    )
    login_url = forms.URLField(
        required=False,
        label="Web del banco",
        help_text="Opcional. Si la conoces, la dejamos ya puesta en el ejemplo del robot.",
        widget=forms.URLInput(
            attrs={
                "placeholder": "https://www.tu-banco.es/login",
            }
        ),
    )

    def clean_bank_name(self):
        return self.cleaned_data["bank_name"].strip()

    def clean_account_label(self):
        return self.cleaned_data["account_label"].strip()
