from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from codito_relay.core.models import Invitation


class Command(BaseCommand):
    help = "Issue a single-use 24-hour invitation and print its manually deliverable URL"

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("email")
        parser.add_argument("--created-by", required=True, help="Administrator username")

    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        User = get_user_model()
        try:
            administrator = User.objects.get(username=options["created_by"], is_staff=True)
        except User.DoesNotExist as exc:
            raise CommandError("The issuing administrator does not exist or is not staff") from exc
        invitation, raw = Invitation.issue(email=options["email"], created_by=administrator)
        self.stdout.write(f"{settings.PUBLIC_BASE_URL}/invite/{raw}/")
        self.stderr.write(f"Invitation {invitation.pk} expires {invitation.expires_at.isoformat()}")
