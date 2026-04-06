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


class PropertyInvestmentForm(forms.Form):
    ownership_category = forms.ChoiceField(
        choices=AssetOwnershipCategory.choices,
        initial=AssetOwnershipCategory.JOINT,
        label="Titular",
    )
    property_name = forms.CharField(max_length=150, label="Inmueble")
    city = forms.CharField(max_length=120, label="Ciudad")
    invested_equity = FlexibleDecimalField(max_digits=14, decimal_places=2, label="Capital invertido")
    market_value = FlexibleDecimalField(max_digits=14, decimal_places=2, label="Valor de mercado")
    mortgage_balance = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Hipoteca pendiente",
    )
    annual_rent_income = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Alquiler anual",
    )
    annual_expenses = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Gastos anuales",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Notas")

    def clean_property_name(self):
        return self.cleaned_data["property_name"].strip()

    def clean_city(self):
        return self.cleaned_data["city"].strip()

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data["mortgage_balance"] = cleaned_data.get("mortgage_balance") or 0
        cleaned_data["annual_rent_income"] = cleaned_data.get("annual_rent_income") or 0
        cleaned_data["annual_expenses"] = cleaned_data.get("annual_expenses") or 0
        return cleaned_data
