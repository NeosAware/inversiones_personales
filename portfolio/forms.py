from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError

from .models import PlannedInvestmentPayment, SalesForecastSnapshot


ACCESS_LEVEL_CHOICES = (
    ("user", "Usuario"),
    ("admin", "Administrador"),
)


class FlexibleDecimalField(forms.DecimalField):
    def to_python(self, value):
        if isinstance(value, str):
            text = value.strip().replace("\xa0", "").replace(" ", "")
            if "," in text:
                text = text.replace(".", "").replace(",", ".")
            value = text
        return super().to_python(value)


class MonthStartField(forms.DateField):
    def to_python(self, value):
        if isinstance(value, str):
            text = value.strip()
            if len(text) == 7 and text[4] == "-":
                value = f"{text}-01"
        return super().to_python(value)


class ManagedUserCreateForm(forms.Form):
    username = forms.CharField(max_length=150, label="Usuario")
    password1 = forms.CharField(widget=forms.PasswordInput, label="Contrasena")
    password2 = forms.CharField(widget=forms.PasswordInput, label="Repetir contrasena")
    access_level = forms.ChoiceField(
        choices=ACCESS_LEVEL_CHOICES,
        initial="user",
        label="Nivel de acceso",
    )

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        User = get_user_model()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("Ya existe un usuario con ese nombre.")
        return username

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Las contrasenas no coinciden.")
            return cleaned_data

        if password1:
            User = get_user_model()
            provisional_user = User(username=cleaned_data.get("username", ""))
            try:
                password_validation.validate_password(password1, provisional_user)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned_data


class PlannedInvestmentPaymentForm(forms.Form):
    title = forms.CharField(
        max_length=180,
        label="Concepto",
        widget=forms.TextInput(attrs={"placeholder": "Ejemplo: compra Iberdrola"}),
    )
    investment_block = forms.ChoiceField(
        choices=PlannedInvestmentPayment.InvestmentBlock.choices,
        initial=PlannedInvestmentPayment.InvestmentBlock.EQUITIES,
        label="Bloque",
    )
    ownership_category = forms.ChoiceField(
        choices=PlannedInvestmentPayment._meta.get_field("ownership_category").choices,
        initial=PlannedInvestmentPayment._meta.get_field("ownership_category").default,
        label="Titular",
    )
    flow_type = forms.ChoiceField(
        choices=PlannedInvestmentPayment.FlowType.choices,
        initial=PlannedInvestmentPayment.FlowType.OUTFLOW,
        label="Tipo",
    )
    due_date = forms.DateField(
        label="Fecha prevista",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    amount = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
        label="Importe",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
    )
    notes = forms.CharField(
        required=False,
        label="Notas",
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Opcional"}),
    )

    def clean_title(self):
        return self.cleaned_data["title"].strip()

    def clean_notes(self):
        return self.cleaned_data["notes"].strip()


class SalesForecastSnapshotForm(forms.Form):
    month = MonthStartField(
        label="Mes",
        widget=forms.DateInput(attrs={"type": "month"}),
    )
    forecast_revenue = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Facturacion prevista",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    forecast_purchase_cost = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Coste compra previsto",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    forecast_units = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        label="Unidades previstas",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    forecast_average_purchase_price = FlexibleDecimalField(
        max_digits=14,
        decimal_places=4,
        required=False,
        label="Precio medio compra",
        widget=forms.NumberInput(attrs={"step": "0.0001", "min": "0"}),
    )
    forecast_average_sale_price = FlexibleDecimalField(
        max_digits=14,
        decimal_places=4,
        required=False,
        label="Precio medio venta",
        widget=forms.NumberInput(attrs={"step": "0.0001", "min": "0"}),
    )
    actual_revenue = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        label="Facturacion real",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    actual_purchase_cost = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        label="Coste compra real",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    notes = forms.CharField(
        required=False,
        label="Notas",
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Origen, hipotesis o comentario"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        units = cleaned_data.get("forecast_units")
        average_purchase_price = cleaned_data.get("forecast_average_purchase_price")
        average_sale_price = cleaned_data.get("forecast_average_sale_price")

        if units and average_sale_price and not cleaned_data.get("forecast_revenue"):
            cleaned_data["forecast_revenue"] = units * average_sale_price
        if units and average_purchase_price and not cleaned_data.get("forecast_purchase_cost"):
            cleaned_data["forecast_purchase_cost"] = units * average_purchase_price

        for field_name in ("forecast_revenue", "forecast_purchase_cost"):
            if cleaned_data.get(field_name) is None:
                cleaned_data[field_name] = 0
        return cleaned_data

    def clean_notes(self):
        return self.cleaned_data["notes"].strip()

    def save(self):
        month = self.cleaned_data["month"].replace(day=1)
        snapshot, _ = SalesForecastSnapshot.objects.update_or_create(
            month=month,
            defaults={
                "forecast_revenue": self.cleaned_data["forecast_revenue"],
                "forecast_purchase_cost": self.cleaned_data["forecast_purchase_cost"],
                "forecast_units": self.cleaned_data.get("forecast_units"),
                "forecast_average_purchase_price": self.cleaned_data.get("forecast_average_purchase_price"),
                "forecast_average_sale_price": self.cleaned_data.get("forecast_average_sale_price"),
                "actual_revenue": self.cleaned_data.get("actual_revenue"),
                "actual_purchase_cost": self.cleaned_data.get("actual_purchase_cost"),
                "notes": self.cleaned_data["notes"],
            },
        )
        return snapshot
