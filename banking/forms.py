from django import forms


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_file_clean(item, initial) for item in data]
        return [single_file_clean(data, initial)]


class StatementUploadForm(forms.Form):
    files = MultipleFileField(
        label="Extractos bancarios",
        widget=MultipleFileInput(attrs={"accept": ".xls,.xlsx"}),
        help_text="Sube uno o varios extractos bancarios en formato XLS o XLSX.",
    )
