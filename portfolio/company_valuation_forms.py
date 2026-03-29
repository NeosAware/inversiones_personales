from django import forms


class AnnualCompanyValuationForm(forms.Form):
    year = forms.IntegerField(min_value=2000, label="Financial year")
    ownership_pct = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
        initial=100,
        label="Your ownership (%)",
    )
    balance_approved = forms.BooleanField(required=False, initial=False, label="Approved balance")
    audited_favorable = forms.BooleanField(required=False, initial=False, label="Favorable audit")
    balance_pdf = forms.FileField(required=False, label="Balance PDF")
    profit_loss_pdf = forms.FileField(required=False, label="P&L PDF")
    corporate_tax_pdf = forms.FileField(required=False, label="IS PDF")
    net_equity = forms.DecimalField(required=False, max_digits=14, decimal_places=2, label="Net equity")
    share_capital = forms.DecimalField(required=False, max_digits=14, decimal_places=2, label="Share capital")
    profit_after_tax = forms.DecimalField(required=False, max_digits=14, decimal_places=2, label="Profit after tax")

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
            raise forms.ValidationError("Upload at least one PDF or enter at least one financial figure for the year.")
        return cleaned_data
