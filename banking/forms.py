from django import forms

from portfolio.ownership import AssetOwnershipCategory

from .models import BankStatementImport


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
            return [single_file_clean(item, initial) for item in data]
        return [single_file_clean(data, initial)]


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
