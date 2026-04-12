from decimal import Decimal

from django import forms

from portfolio.ownership import AssetOwnershipCategory

from .broker_costs import BROKER_TRADE_CHANNEL_CHOICES
from .models import EquityPosition
from .services import apply_equity_company_defaults


class FlexibleDecimalField(forms.DecimalField):
    def to_python(self, value):
        if isinstance(value, str):
            text = value.strip().replace("\xa0", "").replace(" ", "")
            if "," in text:
                text = text.replace(".", "").replace(",", ".")
            value = text
        return super().to_python(value)


class EquityPositionForm(forms.Form):
    position_kind = forms.ChoiceField(
        choices=(
            ("owned", "Comprada"),
            ("watchlist", "En seguimiento"),
        ),
        initial="owned",
        required=False,
        label="Estado",
    )
    ownership_category = forms.ChoiceField(
        choices=AssetOwnershipCategory.choices,
        initial=AssetOwnershipCategory.JOINT,
        label="Titular",
    )
    broker = forms.CharField(max_length=120, required=False, label="Broker o entidad")
    trade_channel = forms.ChoiceField(
        choices=BROKER_TRADE_CHANNEL_CHOICES,
        initial="app",
        required=False,
        label="Canal de compra",
    )
    ticker = forms.CharField(max_length=20, required=False, label="Ticker")
    company_name = forms.CharField(max_length=160, required=False, label="Empresa")
    quote_symbol = forms.CharField(max_length=40, required=False, label="Simbolo de mercado")
    reference_profile = forms.ChoiceField(
        choices=(
            ("market_index", "Indice o activo cotizado"),
            ("euribor_12m", "Euribor 12 meses"),
            ("spain_house_price", "Precio vivienda Espana"),
            ("spain_electricity_demand", "Demanda electrica Espana"),
            ("spain_gas_consumption", "Consumo de gas Espana"),
        ),
        initial="market_index",
        required=False,
        label="Variable de referencia",
    )
    benchmark_symbol = forms.CharField(
        max_length=40,
        required=False,
        initial="^IBEX",
        label="Simbolo de referencia",
    )
    benchmark_name = forms.CharField(
        max_length=120,
        required=False,
        initial="IBEX 35",
        label="Nombre de referencia",
    )
    opened_on = forms.DateField(
        required=False,
        label="Fecha de compra",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    shares = FlexibleDecimalField(
        max_digits=14,
        decimal_places=4,
        required=False,
        label="Acciones compradas",
        help_text="Usa 0 si solo la estas siguiendo y aun no la has comprado.",
    )
    average_cost_per_share = FlexibleDecimalField(
        max_digits=14,
        decimal_places=4,
        required=False,
        label="Precio de compra o referencia",
    )
    current_price_per_share = FlexibleDecimalField(
        max_digits=14,
        decimal_places=4,
        required=False,
        label="Precio actual",
    )
    annual_dividend_income = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Dividendos anuales esperados",
    )
    annual_maintenance_cost = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        initial=0,
        label="Coste manual anual adicional",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Notas")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["broker"].widget.attrs.update(
            {
                "autocomplete": "off",
                "placeholder": "Opcional en seguimiento",
            }
        )
        self.fields["trade_channel"].widget.attrs.update({"data-broker-channel": "1"})
        self.fields["company_name"].widget.attrs.update(
            {
                "list": "equity-company-options",
                "autocomplete": "off",
                "placeholder": "Ejemplo: Indra",
            }
        )
        self.fields["ticker"].widget.attrs.update(
            {
                "autocomplete": "off",
                "placeholder": "Se rellena al reconocer la empresa",
            }
        )
        self.fields["quote_symbol"].widget.attrs.update(
            {
                "autocomplete": "off",
                "placeholder": "Se rellena con el simbolo cotizado",
            }
        )
        self.fields["benchmark_name"].widget.attrs.update(
            {
                "placeholder": "La referencia sugerida se completa sola",
            }
        )
        self.fields["benchmark_symbol"].widget.attrs.update(
            {
                "placeholder": "Simbolo externo o indice",
            }
        )
        self.fields["annual_maintenance_cost"].help_text = (
            "Si lo dejas a 0, la app aplicara la tarifa del banco cuando la conozca; usalo solo para ajustes manuales."
        )
        self.fields["opened_on"].help_text = (
            "Muy recomendable para calcular historico, rentabilidad anualizada y costes acumulados con precision."
        )

    def clean_broker(self):
        return str(self.cleaned_data.get("broker") or "").strip()

    def clean_trade_channel(self):
        value = str(self.cleaned_data.get("trade_channel") or "").strip().lower()
        return value or EquityPosition.TradeChannel.APP

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
        cleaned_data["position_kind"] = cleaned_data.get("position_kind") or "owned"
        cleaned_data["reference_profile"] = cleaned_data.get("reference_profile") or "market_index"
        cleaned_data["trade_channel"] = cleaned_data.get("trade_channel") or EquityPosition.TradeChannel.APP
        raw_reference_profile = str(self.data.get("reference_profile", "")).strip()
        raw_benchmark_symbol = str(self.data.get("benchmark_symbol", "")).strip()
        raw_benchmark_name = str(self.data.get("benchmark_name", "")).strip()
        cleaned_data = apply_equity_company_defaults(
            cleaned_data,
            override_generic_reference=not (
                (raw_reference_profile and raw_reference_profile != EquityPosition.ReferenceProfile.MARKET_INDEX)
                or raw_benchmark_symbol
                or raw_benchmark_name
            ),
        )
        if not cleaned_data.get("company_name") and not cleaned_data.get("ticker"):
            message = "Escribe una empresa reconocida o su ticker."
            self.add_error("company_name", message)
            self.add_error("ticker", message)
        elif not cleaned_data.get("ticker"):
            self.add_error("ticker", "No se ha podido reconocer el ticker automaticamente.")
        elif not cleaned_data.get("company_name"):
            self.add_error("company_name", "Completa el nombre de la empresa.")
        if cleaned_data.get("annual_dividend_income") is None:
            cleaned_data["annual_dividend_income"] = 0
        if cleaned_data.get("annual_maintenance_cost") is None:
            cleaned_data["annual_maintenance_cost"] = 0
        if cleaned_data.get("position_kind") == "watchlist":
            if not cleaned_data.get("broker"):
                cleaned_data["broker"] = "Seguimiento"
            if not cleaned_data.get("trade_channel"):
                cleaned_data["trade_channel"] = EquityPosition.TradeChannel.APP
            if cleaned_data.get("shares") is None:
                cleaned_data["shares"] = Decimal("0")
            if cleaned_data.get("average_cost_per_share") is None and cleaned_data.get("current_price_per_share") is not None:
                cleaned_data["average_cost_per_share"] = cleaned_data.get("current_price_per_share")
            if cleaned_data.get("current_price_per_share") is None and cleaned_data.get("average_cost_per_share") is not None:
                cleaned_data["current_price_per_share"] = cleaned_data.get("average_cost_per_share")
            if cleaned_data.get("average_cost_per_share") is None:
                cleaned_data["average_cost_per_share"] = Decimal("0")
            if cleaned_data.get("current_price_per_share") is None:
                cleaned_data["current_price_per_share"] = Decimal("0")
        else:
            if not cleaned_data.get("broker"):
                self.add_error("broker", "Indica el broker o la entidad donde has comprado la posicion.")
            if cleaned_data.get("shares") is None:
                self.add_error("shares", "Indica cuantas acciones has comprado.")
            if cleaned_data.get("average_cost_per_share") is None:
                self.add_error("average_cost_per_share", "Indica el precio medio de compra.")
            if cleaned_data.get("current_price_per_share") is None:
                cleaned_data["current_price_per_share"] = cleaned_data.get("average_cost_per_share")
        if (
            cleaned_data.get("reference_profile") == EquityPosition.ReferenceProfile.MARKET_INDEX
            and not cleaned_data.get("benchmark_symbol")
        ):
            self.add_error("benchmark_symbol", "Completa el simbolo de referencia o usa una sugerencia.")
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


class EquityAllocationOptimizerForm(forms.Form):
    total_investment = FlexibleDecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        min_value=Decimal("1"),
        label="Capital total a invertir",
    )
    max_company_pct = FlexibleDecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        min_value=Decimal("1"),
        max_value=Decimal("100"),
        label="Peso maximo por empresa (%)",
    )
    max_total_positions = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=35,
        label="Maximo total de empresas",
        help_text="Pon 0 para no limitar. Si pones 8, la propuesta no elegira mas de 8 valores distintos.",
    )
    max_sector_positions = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=35,
        label="Maximo de empresas por sector",
        help_text="Pon 0 para no limitar sectores; si pones 1 solo entrara la mejor empresa de cada sector.",
    )
    selected_sectors = forms.MultipleChoiceField(
        required=False,
        choices=(),
        label="Sectores donde si comprar",
        help_text="Si no marcas ninguno, la optimizacion puede elegir cualquier sector del IBEX.",
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        default_total_investment = kwargs.pop("default_total_investment", Decimal("100000"))
        sector_choices = kwargs.pop("sector_choices", ())
        super().__init__(*args, **kwargs)
        self.fields["total_investment"].initial = default_total_investment
        self.fields["max_company_pct"].initial = Decimal("20")
        self.fields["max_total_positions"].initial = 0
        self.fields["max_sector_positions"].initial = 0
        self.fields["selected_sectors"].choices = sector_choices
        self.fields["total_investment"].widget.attrs.update(
            {
                "placeholder": "Ejemplo: 100000",
            }
        )
        self.fields["max_company_pct"].widget.attrs.update(
            {
                "placeholder": "Ejemplo: 20",
            }
        )
        self.fields["max_total_positions"].widget.attrs.update(
            {
                "placeholder": "0 sin limite, 8 maximo",
            }
        )
        self.fields["max_sector_positions"].widget.attrs.update(
            {
                "placeholder": "0 sin limite, 1 por sector",
            }
        )

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("total_investment") is None:
            cleaned_data["total_investment"] = self.fields["total_investment"].initial or Decimal("100000")
        if cleaned_data.get("max_company_pct") is None:
            cleaned_data["max_company_pct"] = self.fields["max_company_pct"].initial or Decimal("20")
        if cleaned_data.get("max_total_positions") is None:
            cleaned_data["max_total_positions"] = self.fields["max_total_positions"].initial or 0
        if cleaned_data.get("max_sector_positions") is None:
            cleaned_data["max_sector_positions"] = self.fields["max_sector_positions"].initial or 0
        cleaned_data["selected_sectors"] = [
            str(sector or "").strip()
            for sector in cleaned_data.get("selected_sectors") or []
            if str(sector or "").strip()
        ]
        return cleaned_data


class EquityOptimizationRunForm(EquityAllocationOptimizerForm):
    reference_label = forms.CharField(
        max_length=160,
        required=False,
        label="Referencia de la optimizacion",
        help_text="Opcional. Si no la rellenas, la app generara una referencia automatica con fecha y hora.",
    )
    restrictions_note = forms.CharField(
        required=False,
        label="Restricciones y notas",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Ejemplo: priorizar dividendos, evitar bancos, cartera conservadora o cualquier condicion adicional.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reference_label"].widget.attrs.update(
            {
                "placeholder": "Ejemplo: Cartera defensiva abril 2026",
            }
        )

    def clean_reference_label(self):
        return str(self.cleaned_data.get("reference_label") or "").strip()

    def clean_restrictions_note(self):
        return str(self.cleaned_data.get("restrictions_note") or "").strip()


class EquityClosePositionForm(forms.Form):
    closed_on = forms.DateField(
        label="Fecha de venta",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    sale_price_per_share = FlexibleDecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0"),
        label="Precio de venta por accion",
    )
    notes = forms.CharField(
        required=False,
        label="Notas de venta",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def clean_notes(self):
        return str(self.cleaned_data.get("notes") or "").strip()
