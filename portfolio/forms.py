from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError


ACCESS_LEVEL_CHOICES = (
    ("user", "Usuario"),
    ("admin", "Administrador"),
)


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
