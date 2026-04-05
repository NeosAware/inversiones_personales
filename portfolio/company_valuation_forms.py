from django import forms


class AnnualCompanyValuationForm(forms.Form):
    year = forms.IntegerField(min_value=2000, label="Ejercicio")
    ownership_pct = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
        initial=100,
        label="Tu participacion (%)",
    )
    balance_approved = forms.BooleanField(required=False, initial=False, label="Balance aprobado")
    audited_favorable = forms.BooleanField(required=False, initial=False, label="Auditoria favorable")
    balance_pdf = forms.FileField(required=False, label="PDF del balance")
    profit_loss_pdf = forms.FileField(required=False, label="PDF de PyG")
    corporate_tax_pdf = forms.FileField(required=False, label="PDF del IS")
    net_equity = forms.DecimalField(required=False, max_digits=14, decimal_places=2, label="Patrimonio neto")
    share_capital = forms.DecimalField(required=False, max_digits=14, decimal_places=2, label="Capital social")
    profit_after_tax = forms.DecimalField(required=False, max_digits=14, decimal_places=2, label="Resultado del ejercicio")

    def clean(self):
        cleaned_data = super().clean()
        payload_fields = (
            "balance_pdf",
            "profit_loss_pdf",
            "corporate_tax_pdf",
            "net_equity",
            "share_capital",
            "profit_after_tax",
        )
        if not any(cleaned_data.get(field_name) for field_name in payload_fields):
            raise forms.ValidationError("Sube al menos un PDF o introduce al menos una magnitud financiera del ejercicio.")
        return cleaned_data
