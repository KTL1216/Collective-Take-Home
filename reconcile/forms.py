from django import forms


class ReconciliationUploadForm(forms.Form):
    """Paired CSV uploads; 2 MB cap aligns with FILE_UPLOAD_MAX_MEMORY_SIZE."""
    transactions_file = forms.FileField(
        label="Transactions CSV",
        help_text=(
            "User transaction ledger. Required columns: date, amount. "
            "Multiple rows per day are allowed."
        ),
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )
    bank_balances_file = forms.FileField(
        label="Daily bank statement balances CSV",
        help_text=(
            "End-of-day balances from the bank. Required columns: date, balance."
        ),
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )

    def clean_transactions_file(self):
        uploaded = self.cleaned_data["transactions_file"]
        # Extension check: browsers often send CSV as application/octet-stream.
        if not uploaded.name.lower().endswith(".csv"):
            raise forms.ValidationError("Please upload a .csv file for transactions.")
        if uploaded.size > 2 * 1024 * 1024:
            raise forms.ValidationError("Transactions file must be under 2 MB.")
        return uploaded

    def clean_bank_balances_file(self):
        uploaded = self.cleaned_data["bank_balances_file"]
        # Same rules as transactions_file.
        if not uploaded.name.lower().endswith(".csv"):
            raise forms.ValidationError("Please upload a .csv file for bank balances.")
        if uploaded.size > 2 * 1024 * 1024:
            raise forms.ValidationError("Bank balances file must be under 2 MB.")
        return uploaded
