from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from oauth2_provider.models import Application


class Command(BaseCommand):
    help = "Provision the pre-registered public OIDC client used by the Windows desktop agent"

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback")
        parser.add_argument("--client-id", default=settings.DESKTOP_OAUTH_CLIENT_ID)

    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        redirect_uri = options["redirect_uri"]
        client_id = options["client_id"]
        if not redirect_uri.startswith("http://127.0.0.1:"):
            raise CommandError("The desktop redirect must be an IPv4 loopback URI")
        application, created = Application.objects.get_or_create(
            client_id=client_id,
            defaults={
                "name": "Codito Windows Agent",
                "client_type": Application.CLIENT_PUBLIC,
                "authorization_grant_type": Application.GRANT_AUTHORIZATION_CODE,
                "redirect_uris": redirect_uri,
                "hash_client_secret": False,
                "client_secret": "",
                "algorithm": Application.RS256_ALGORITHM,
            },
        )
        if not created and (
            application.name != "Codito Windows Agent"
            or application.redirect_uris != redirect_uri
            or application.client_type != Application.CLIENT_PUBLIC
            or application.authorization_grant_type != Application.GRANT_AUTHORIZATION_CODE
            or application.algorithm != Application.RS256_ALGORITHM
        ):
            raise CommandError(
                "Desktop application exists with incompatible redirect, grant, type, or signing"
            )
        self.stdout.write(application.client_id)
