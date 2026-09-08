from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.contrib.auth.forms import SetPasswordForm, UserCreationForm
from django.contrib.auth.models import User

from .models import Invitation

if TYPE_CHECKING:
    SetPasswordFormBase = SetPasswordForm[User]
else:
    SetPasswordFormBase = SetPasswordForm


class InvitationRegistrationForm(UserCreationForm[User]):
    email = forms.EmailField(disabled=True)

    class Meta:
        model = User
        fields = ("email", "username", "password1", "password2")

    def __init__(self, *args: object, invitation: Invitation, **kwargs: object) -> None:
        self.invitation = invitation
        super().__init__(*args, **kwargs)
        self.fields["email"].initial = invitation.email

    def save(self, commit: bool = True):  # type: ignore[no-untyped-def]
        user = super().save(commit=False)
        user.email = self.invitation.email
        if commit:
            user.save()
        return user

    def clean_email(self) -> str:
        email = self.invitation.email
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account already uses this invitation email")
        return email


class AdminResetPasswordForm(SetPasswordFormBase):
    pass
