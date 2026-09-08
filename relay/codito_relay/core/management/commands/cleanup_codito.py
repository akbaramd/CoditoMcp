from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from codito_relay.core.models import (
    AuditEvent,
    DeviceConnectionTicket,
    Invitation,
    Operation,
    PasswordResetLink,
)


class Command(BaseCommand):
    help = "Remove expired ephemeral records and relay history beyond the 30-day retention window"

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        now = timezone.now()
        retention = now - timedelta(days=30)
        querysets = {
            "device_tickets": DeviceConnectionTicket.objects.filter(expires_at__lt=now),
            "used_or_expired_invitations": Invitation.objects.filter(expires_at__lt=retention),
            "password_reset_links": PasswordResetLink.objects.filter(expires_at__lt=now),
            "operations": Operation.objects.filter(created_at__lt=retention),
            "audit_events": AuditEvent.objects.filter(occurred_at__lt=retention),
        }
        counts = {name: queryset.count() for name, queryset in querysets.items()}
        if options["dry_run"]:
            transaction.set_rollback(True)
        else:
            for queryset in querysets.values():
                queryset.delete()
        self.stdout.write(" ".join(f"{name}={count}" for name, count in counts.items()))
