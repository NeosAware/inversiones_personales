from django import forms

from portfolio.ownership import AssetOwnershipCategory


class FlexibleDecimalField(forms.DecimalField):
    def to_python(self, value):
        if isinstance(value, str):
            text = value.strip().replace("\xa0", "").replace(" ", "")
            if "," in text:
                text = text.replace(".", "").replace(",", ".")
            value = text
        return super().to_python(value)


class EquityPositionForm(forms.Form):
    ownership_category = forms.ChoiceField(
        choices=AssetOwnershipCategory.choices,
        initial=AssetOwnershipCategory.JOINT,
        label="Titular",
    )
    broker = forms.CharField(max_length=120, label="Broker o entidad")
    ticker = forms.CharField(max_length=20, label="Ticker")
    company_name = forms.CharField(max_length=160, label="Empresa")
    quote_symbol = forms.CharField(max_length=40, required=False, label="Simbolo de mercado")
    benchmark_symbol = forms.CharField(max_length=40, required=False, initial="^IBEX", label="Indice de referencia")
    benchmark_name = forms.CharField(max_length=120, required=False, initial="IBEX 35", label="Nombre del indice")
    shares = FlexibleDecimalField(max_digits=14, decimal_places=4, label="Acciones")
    average_cost_per_share = FlexibleDecimalField(max_digits=14, decimal_places=4, label="Coste medio por accion")
    current_price_per_share = FlexibleDecimalField(
        max_digits=14,
        decimal_places=4,
        required=False,
        label="Precio actual por accion",
    )
    annual_dividend_income = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Dividendos anuales",
    )
    annual_maintenance_cost = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Coste anual de mantenimiento",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Notas")

    def clean_broker(self):
        return self.cleaned_data["broker"].strip()

    def clean_ticker(self):
        return self.cleaned_data["ticker"].strip().upper()

    def clean_company_name(self):
        return self.cleaned_data["company_name"].strip()

    def clean_quote_symbol(self):
        return self.cleaned_data["quote_symbol"].strip().upper()

    def clean_benchmark_symbol(self):
        value = self.cleaned_data["benchmark_symbol"].strip().upper()
        return value or "^IBEX"

    def clean_benchmark_name(self):
        value = self.cleaned_data["benchmark_name"].strip()
        return value or "IBEX 35"

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("current_price_per_share") is None:
            cleaned_data["current_price_per_share"] = cleaned_data.get("average_cost_per_share")
        if cleaned_data.get("annual_dividend_income") is None:
            cleaned_data["annual_dividend_income"] = 0
        if cleaned_data.get("annual_maintenance_cost") is None:
            cleaned_data["annual_maintenance_cost"] = 0
        return cleaned_data


class EquityDocumentImportForm(forms.Form):
    document = forms.FileField(
        label="Documento de acciones",
        widget=forms.ClearableFileInput(attrs={"accept": ".xls,.xlsx,.pdf"}),
        help_text="Sube un XLS, XLSX o PDF con una posicion para rellenar el formulario.",
    )
    default_broker = forms.CharField(
        max_length=120,
        required=False,
        label="Broker por defecto",
        help_text="Se usa si el documento no indica la entidad o broker.",
    )
    default_ownership_category = forms.ChoiceField(
        choices=AssetOwnershipCategory.choices,
        initial=AssetOwnershipCategory.JOINT,
        label="Titular por defecto",
    )

    def clean_default_broker(self):
        return self.cleaned_data["default_broker"].strip()
