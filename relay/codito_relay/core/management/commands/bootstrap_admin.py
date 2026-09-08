from __future__ import annotations

import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Create the one-time initial Codito administrator interactively"

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--email")
        parser.add_argument("--username")

    @transaction.atomic
    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        User = get_user_model()
        if User.objects.filter(is_superuser=True).exists():
            raise CommandError(
                "An administrator already exists; use Django admin for further staff accounts"
            )
        email = options.get("email") or input("Email: ").strip()
        username = options.get("username") or input("Username: ").strip()
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if not email or not username or password != confirmation:
            raise CommandError("Email/username are required and passwords must match")
        provisional = User(username=username, email=email)
        validate_password(password, user=provisional)
        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"Created initial administrator {email}"))
