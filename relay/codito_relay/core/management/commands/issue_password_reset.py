from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from codito_relay.core.models import PasswordResetLink


class Command(BaseCommand):
    help = "Issue a single-use 15-minute password reset link for manual delivery"

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("email")
        parser.add_argument("--created-by", required=True, help="Administrator username")

    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        User = get_user_model()
        try:
            administrator = User.objects.get(username=options["created_by"], is_staff=True)
            user = User.objects.get(email__iexact=options["email"], is_active=True)
        except User.DoesNotExist as exc:
            raise CommandError("Administrator or target account not found") from exc
        reset, raw = PasswordResetLink.issue(user=user, created_by=administrator)
        self.stdout.write(f"{settings.PUBLIC_BASE_URL}/reset/{raw}/")
        self.stderr.write(f"Reset {reset.pk} expires {reset.expires_at.isoformat()}")
