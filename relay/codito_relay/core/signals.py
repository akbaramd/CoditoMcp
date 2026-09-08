from __future__ import annotations

import hashlib
import uuid
from typing import Any

from django.core.exceptions import SuspiciousOperation
from django.db import transaction
from oauth2_provider.models import AccessToken, RefreshToken
from oauth2_provider.signals import app_authorized

from .models import DeviceLink, OAuthGrantBinding, OAuthGrantFamily
from .oauth import is_allowed_mcp_application

MCP_SCOPES = frozenset({"projects:read", "files:read", "files:write", "shell:execute"})


def _request_value(request: Any, name: str) -> str:
    django_request = getattr(request, "django_request", None) or getattr(request, "request", None)
    for source in (request, django_request):
        if source is None:
            continue
        value = getattr(source, name, None)
        if value:
            if isinstance(value, (list, tuple)):
                return str(value[0]) if len(value) == 1 else ""
            return str(value)
        post = getattr(source, "POST", None)
        if post is not None and post.get(name):
            values = post.getlist(name)
            return str(values[0]) if len(values) == 1 else ""
    return ""


def bind_issued_access_token(*, token: AccessToken, request: Any) -> OAuthGrantBinding | None:
    """Fail closed and persist the exact MCP resource/audience for each issued tool token."""

    scopes = frozenset(token.scope.split())
    if not scopes.intersection(MCP_SCOPES):
        return None
    if not is_allowed_mcp_application(token.application):
        token.delete()
        raise SuspiciousOperation("This OAuth application class may not receive MCP tokens")

    token_resources = list(token.resource or [])
    requested_resource = _request_value(request, "resource")
    if len(token_resources) != 1 or (
        requested_resource and requested_resource != token_resources[0]
    ):
        token.delete()
        raise SuspiciousOperation("MCP OAuth tokens require exactly one resource indicator")
    resource = token_resources[0]
    try:
        link = DeviceLink.objects.select_related("device").get(
            link_id=resource.rstrip("/").rsplit("/", 1)[-1], revoked_at__isnull=True
        )
    except (DeviceLink.DoesNotExist, ValueError) as exc:
        token.delete()
        raise SuspiciousOperation("Unknown MCP resource") from exc
    if resource != link.resource or token.user_id != link.account_id or not link.device.active:
        token.delete()
        raise SuspiciousOperation("MCP token account/resource/device binding is invalid")

    scopes_digest = hashlib.sha256(" ".join(sorted(scopes)).encode()).hexdigest()
    grant_id = str(uuid.uuid4())
    source_refresh = _request_value(request, "refresh_token")
    previous = None
    if source_refresh:
        previous = (
            RefreshToken.objects.filter(
                token_checksum=hashlib.sha256(source_refresh.encode()).hexdigest()
            )
            .order_by("-created")
            .first()
        )

    current_refresh = RefreshToken.objects.filter(access_token=token).first()
    token_family = (
        previous.token_family
        if previous is not None and previous.token_family is not None
        else current_refresh.token_family
        if current_refresh is not None
        else None
    )
    try:
        with transaction.atomic():
            family = None
            if token_family is not None:
                family = (
                    OAuthGrantFamily.objects.select_for_update()
                    .filter(token_family=token_family)
                    .first()
                )
            if family is not None:
                if (
                    family.account_id != token.user_id
                    or family.application_id != token.application_id
                    or family.device_link_id != link.pk
                    or family.resource != resource
                ):
                    raise SuspiciousOperation(
                        "Refresh token account/client/resource substitution was rejected"
                    )
                grant_id = family.oauth_grant_id

            binding = OAuthGrantBinding(
                access_token=token,
                account_id=token.user_id,
                device_link=link,
                resource=resource,
                scopes_digest=scopes_digest,
                oauth_grant_id=grant_id,
            )
            binding.full_clean()
            binding.save()

            if token_family is not None and family is None:
                family = OAuthGrantFamily(
                    token_family=token_family,
                    account_id=token.user_id,
                    application_id=token.application_id,
                    device_link=link,
                    resource=resource,
                    scopes_digest=scopes_digest,
                    oauth_grant_id=grant_id,
                )
                family.full_clean()
                family.save()
            return binding
    except Exception:
        token.delete()
        raise


def _on_app_authorized(sender: Any, request: Any, token: AccessToken, **kwargs: Any) -> None:
    bind_issued_access_token(token=token, request=request)


app_authorized.connect(_on_app_authorized, dispatch_uid="codito-bind-mcp-resource")
