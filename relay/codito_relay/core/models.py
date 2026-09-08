from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from oauth2_provider.models import AccessToken

User = get_user_model()


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_project_id() -> str:
    return "project_" + secrets.token_urlsafe(18)


class Invitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_invitations"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_invitation",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.email

    @classmethod
    def issue(cls, *, email: str, created_by: Any) -> tuple[Invitation, str]:
        raw = secrets.token_urlsafe(32)
        invitation = cls.objects.create(
            email=email.strip().lower(),
            token_hash=hash_secret(raw),
            created_by=created_by,
            expires_at=timezone.now() + timedelta(hours=24),
        )
        return invitation, raw

    @property
    def usable(self) -> bool:
        return not self.used_at and not self.revoked_at and self.expires_at > timezone.now()

    def matches(self, raw_token: str) -> bool:
        return secrets.compare_digest(self.token_hash, hash_secret(raw_token))


class PasswordResetLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_password_resets"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Password reset for user {self.user_id}"

    @classmethod
    def issue(cls, *, user: Any, created_by: Any) -> tuple[PasswordResetLink, str]:
        raw = secrets.token_urlsafe(32)
        reset = cls.objects.create(
            user=user,
            token_hash=hash_secret(raw),
            created_by=created_by,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        return reset, raw

    @property
    def usable(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()


class Device(models.Model):
    class Status(models.TextChoices):
        ENROLLING = "enrolling", "Enrolling"
        ONLINE = "online", "Online"
        OFFLINE = "offline", "Offline"
        REVOKED = "revoked", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="devices"
    )
    name = models.CharField(max_length=120)
    public_key_jwk = models.JSONField(default=dict)
    key_thumbprint = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ENROLLING)
    connection_epoch = models.PositiveBigIntegerField(default=0)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "name"],
                condition=models.Q(revoked_at__isnull=True),
                name="unique_active_device_name_per_account",
            ),
            models.UniqueConstraint(
                fields=["account", "key_thumbprint"],
                condition=~models.Q(key_thumbprint=""),
                name="unique_device_key_per_account",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def active(self) -> bool:
        return self.revoked_at is None and self.status != self.Status.REVOKED


class DeviceLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    link_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="device_links"
    )
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="links")
    label = models.CharField(max_length=120, default="ChatGPT")
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.label} · {self.device_id}"

    def clean(self) -> None:
        if self.device_id and self.account_id != self.device.account_id:
            raise ValidationError("Device link account must own the device")

    @property
    def active(self) -> bool:
        return self.revoked_at is None and self.device.active

    @property
    def resource(self) -> str:
        return f"{settings.PUBLIC_BASE_URL}/mcp/d/{self.link_id}"


class Project(models.Model):
    class Mode(models.TextChoices):
        ISOLATED = "isolated", "Isolated"
        NATIVE_APPROVAL = "native_approval", "Native approval"
        NATIVE_TRUSTED = "native_trusted", "Native trusted"

    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        UNAVAILABLE = "unavailable", "Unavailable"
        REVOKED = "revoked", "Revoked"

    id = models.CharField(primary_key=True, max_length=128, default=new_project_id, editable=False)
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="projects"
    )
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="projects")
    title = models.CharField(max_length=160)
    root_fingerprint = models.CharField(max_length=128)
    mode = models.CharField(max_length=24, choices=Mode.choices, default=Mode.ISOLATED)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AVAILABLE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device", "root_fingerprint"], name="unique_project_root_per_device"
            ),
        ]
        indexes = [models.Index(fields=["account", "device", "status"])]

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        if self.device_id and self.account_id != self.device.account_id:
            raise ValidationError("Project account must own the device")


class OAuthGrantBinding(models.Model):
    """Binds an opaque DOT token to one account, device link, and exact RFC 8707 resource."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    access_token = models.OneToOneField(
        AccessToken, on_delete=models.CASCADE, related_name="codito_binding"
    )
    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    device_link = models.ForeignKey(
        DeviceLink, on_delete=models.CASCADE, related_name="grant_bindings"
    )
    resource = models.URLField(max_length=500)
    scopes_digest = models.CharField(max_length=64)
    oauth_grant_id = models.CharField(max_length=128, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.oauth_grant_id

    def clean(self) -> None:
        expected_resource = self.device_link.resource if self.device_link_id else ""
        if self.account_id != self.device_link.account_id:
            raise ValidationError("Grant account must own the device link")
        if self.access_token.user_id != self.account_id:
            raise ValidationError("Token user must match the grant account")
        if self.resource != expected_resource:
            raise ValidationError("Grant resource must exactly match the device MCP URL")


class OAuthGrantFamily(models.Model):
    """Durable identity for one rotating refresh-token family.

    DOT deletes an access token (and therefore its ``OAuthGrantBinding``) while
    rotating a refresh token.  Keeping the grant identity against DOT's stable
    ``token_family`` prevents a refreshed token from silently becoming a new
    authorization grant.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_family = models.UUIDField(unique=True, db_index=True)
    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    application = models.ForeignKey("oauth2_provider.Application", on_delete=models.CASCADE)
    device_link = models.ForeignKey(
        DeviceLink, on_delete=models.CASCADE, related_name="oauth_grant_families"
    )
    resource = models.URLField(max_length=500)
    scopes_digest = models.CharField(max_length=64)
    oauth_grant_id = models.CharField(max_length=128, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.oauth_grant_id

    def clean(self) -> None:
        expected_resource = self.device_link.resource if self.device_link_id else ""
        if self.account_id != self.device_link.account_id:
            raise ValidationError("Grant family account must own the device link")
        if self.resource != expected_resource:
            raise ValidationError("Grant family resource must exactly match the device MCP URL")


class DeviceConnectionTicket(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="connection_tickets")
    token_hash = models.CharField(max_length=64, unique=True)
    challenge = models.CharField(max_length=86)
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Ticket {self.pk} for {self.device_id}"

    @classmethod
    def issue(cls, *, device: Device) -> tuple[DeviceConnectionTicket, str]:
        raw = secrets.token_urlsafe(32)
        challenge = secrets.token_urlsafe(48)
        ticket = cls.objects.create(
            device=device,
            token_hash=hash_secret(raw),
            challenge=challenge,
            expires_at=timezone.now() + timedelta(seconds=settings.WS_TICKET_TTL_SECONDS),
        )
        return ticket, raw

    @property
    def usable(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now() and self.device.active


class Operation(models.Model):
    class Kind(models.TextChoices):
        READ = "project_read", "Project read"
        PATCH = "project_apply_patch", "Project apply patch"
        SHELL = "project_shell", "Project shell"
        MANAGE = "project_manage", "Project management"

    class Status(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        DISPATCHED = "dispatched", "Dispatched"
        RECEIVED = "received", "Received"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        OUTCOME_UNKNOWN = "outcome_unknown", "Outcome unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    oauth_grant_id = models.CharField(max_length=128, db_index=True)
    device = models.ForeignKey(Device, on_delete=models.PROTECT)
    device_link = models.ForeignKey(DeviceLink, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, null=True, blank=True)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.ACCEPTED)
    message_id = models.UUIDField(default=uuid.uuid4, unique=True)
    correlation_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    connection_epoch = models.PositiveBigIntegerField()
    idempotency_key = models.CharField(max_length=128, blank=True)
    action_digest = models.CharField(max_length=64)
    request_digest = models.CharField(max_length=64)
    request_payload = models.JSONField(default=dict)
    deadline_at = models.DateTimeField(db_index=True)
    delivery_attempted_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_epoch = models.PositiveBigIntegerField(null=True, blank=True)
    last_device_sequence = models.PositiveBigIntegerField(default=0)
    last_device_sequence_epoch = models.PositiveBigIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "account",
                    "oauth_grant_id",
                    "device_link",
                    "device",
                    "project",
                    "kind",
                    "idempotency_key",
                ],
                condition=~models.Q(idempotency_key=""),
                name="unique_operation_idempotency_scope",
            ),
            models.UniqueConstraint(
                fields=[
                    "account",
                    "oauth_grant_id",
                    "device_link",
                    "device",
                    "kind",
                    "idempotency_key",
                ],
                condition=models.Q(project__isnull=True) & ~models.Q(idempotency_key=""),
                name="unique_device_operation_idempotency_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["device", "status", "created_at"]),
            models.Index(fields=["account", "device_link", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.pk}"


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    actor_type = models.CharField(max_length=24)
    actor_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=80, db_index=True)
    device = models.ForeignKey(Device, on_delete=models.SET_NULL, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True)
    operation = models.ForeignKey(Operation, on_delete=models.SET_NULL, null=True, blank=True)
    metadata = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["account", "occurred_at"])]

    def __str__(self) -> str:
        return f"{self.event_type} · {self.occurred_at}"

    def clean(self) -> None:
        forbidden = {
            "arguments",
            "authorization",
            "command",
            "content",
            "environment",
            "password",
            "patch",
            "path",
            "script",
            "secret",
            "stderr",
            "stdout",
            "token",
        }

        def sensitive_keys(value: Any) -> set[str]:
            if isinstance(value, dict):
                found: set[str] = set()
                for raw_key, nested_value in value.items():
                    key = str(raw_key).lower().replace("-", "_")
                    key_parts = set(key.split("_"))
                    if key in forbidden or forbidden.intersection(key_parts):
                        found.add(str(raw_key))
                    found.update(sensitive_keys(nested_value))
                return found
            if isinstance(value, list):
                return {key for item in value for key in sensitive_keys(item)}
            return set()

        bad = sensitive_keys(self.metadata)
        if bad:
            raise ValidationError(f"Audit metadata contains forbidden fields: {sorted(bad)}")
