from django import forms

from portfolio.ownership import AssetOwnershipCategory

from .models import BankConnection, BankExternalAccount, BankStatementImport


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


class BankInstitutionSearchForm(forms.Form):
    country_code = forms.CharField(
        max_length=2,
        initial="ES",
        label="Pais",
        help_text="Codigo ISO del pais. Para Espana usa ES.",
    )
    query = forms.CharField(
        max_length=120,
        label="Banco",
        help_text="Escribe una parte del nombre del banco para buscarlo en Open Banking.",
    )

    def clean_country_code(self):
        return self.cleaned_data["country_code"].strip().upper()

    def clean_query(self):
        return self.cleaned_data["query"].strip()


class BankConnectionForm(forms.Form):
    ownership_category = forms.ChoiceField(
        choices=AssetOwnershipCategory.choices,
        initial=AssetOwnershipCategory.JOINT,
        label="Titular",
    )
    country_code = forms.CharField(
        max_length=2,
        initial="ES",
        label="Pais",
    )
    institution_id = forms.CharField(max_length=160, label="ID del banco")
    institution_name = forms.CharField(max_length=160, label="Banco")

    def clean_country_code(self):
        return self.cleaned_data["country_code"].strip().upper()

    def clean_institution_id(self):
        return self.cleaned_data["institution_id"].strip()

    def clean_institution_name(self):
        return self.cleaned_data["institution_name"].strip()


class BankExternalAccountForm(forms.Form):
    ownership_category = forms.ChoiceField(
        choices=AssetOwnershipCategory.choices,
        label="Titular",
    )
    statement_kind = forms.ChoiceField(
        choices=BankStatementImport.StatementKind.choices,
        label="Tipo",
    )
    is_active = forms.BooleanField(
        required=False,
        initial=True,
        label="Activa",
    )


class BankConnectionSyncForm(forms.Form):
    connection_id = forms.IntegerField(widget=forms.HiddenInput)

    def clean_connection_id(self):
        return int(self.cleaned_data["connection_id"])
