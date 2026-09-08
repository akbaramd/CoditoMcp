from __future__ import annotations

from django.contrib import admin
from django.utils import timezone

from .models import (
    AuditEvent,
    Device,
    DeviceConnectionTicket,
    DeviceLink,
    Invitation,
    OAuthGrantBinding,
    Operation,
    PasswordResetLink,
    Project,
)


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin[Invitation]):
    list_display = ("email", "created_by", "created_at", "expires_at", "used_at", "revoked_at")
    search_fields = ("email",)
    readonly_fields = ("token_hash", "created_at", "used_at", "used_by")
    actions = ("revoke",)

    @admin.action(description="Revoke selected invitations")
    def revoke(self, request, queryset):  # type: ignore[no-untyped-def]
        queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin[Device]):
    list_display = ("name", "account", "status", "connection_epoch", "last_seen_at", "revoked_at")
    list_filter = ("status",)
    search_fields = ("name", "account__email")
    readonly_fields = (
        "public_key_jwk",
        "key_thumbprint",
        "connection_epoch",
        "last_seen_at",
        "created_at",
    )


@admin.register(DeviceLink)
class DeviceLinkAdmin(admin.ModelAdmin[DeviceLink]):
    list_display = ("link_id", "device", "account", "label", "created_at", "revoked_at")
    readonly_fields = ("link_id", "created_at")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin[Project]):
    list_display = ("title", "device", "account", "mode", "status", "updated_at")
    list_filter = ("mode", "status")
    search_fields = ("title", "account__email", "root_fingerprint")
    readonly_fields = ("root_fingerprint", "created_at", "updated_at")


@admin.register(Operation)
class OperationAdmin(admin.ModelAdmin[Operation]):
    list_display = ("id", "kind", "status", "account", "device", "created_at")
    list_filter = ("kind", "status")
    readonly_fields = tuple(field.name for field in Operation._meta.fields)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin[AuditEvent]):
    list_display = ("event_type", "account", "actor_type", "occurred_at")
    list_filter = ("event_type", "actor_type")
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)


admin.site.register(OAuthGrantBinding)
admin.site.register(DeviceConnectionTicket)
admin.site.register(PasswordResetLink)
