from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from django.utils import timezone
from oauth2_provider.models import AccessToken

from .database_async import database_sync
from .models import DeviceLink, OAuthGrantBinding


class AuthorizationFailure(Exception):
    def __init__(self, code: str, description: str, *, status: int = 401) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.status = status


@dataclass(frozen=True, slots=True)
class MCPPrincipal:
    account_id: int
    oauth_grant_id: str
    access_token_id: int
    device_id: UUID
    device_link_id: UUID
    link_id: UUID
    resource: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AuthorizationFailure(
                "insufficient_scope", f"The OAuth grant does not include {scope}", status=403
            )


def _bearer(headers: Mapping[str, str]) -> str:
    authorization = headers.get("authorization", "")
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not value.strip():
        raise AuthorizationFailure("invalid_token", "A bearer access token is required")
    return value.strip()


def _authenticate(raw_token: str, link_id: UUID) -> MCPPrincipal:
    try:
        token = AccessToken.objects.select_related(
            "user", "codito_binding__device_link__device"
        ).get(token_checksum=__import__("hashlib").sha256(raw_token.encode()).hexdigest())
    except AccessToken.DoesNotExist as exc:
        raise AuthorizationFailure("invalid_token", "The access token is unknown") from exc
    if token.expires <= timezone.now() or token.user_id is None:
        raise AuthorizationFailure("invalid_token", "The access token is expired or unbound")
    try:
        binding: OAuthGrantBinding = token.codito_binding
    except OAuthGrantBinding.DoesNotExist as exc:
        raise AuthorizationFailure(
            "invalid_token", "The access token has no Codito resource binding"
        ) from exc
    link: DeviceLink = binding.device_link
    if binding.revoked_at is not None or link.revoked_at is not None or not link.device.active:
        raise AuthorizationFailure("invalid_token", "The grant, device link, or device was revoked")
    if link.link_id != link_id:
        raise AuthorizationFailure(
            "invalid_target", "The token was issued for another MCP resource", status=403
        )
    if token.user_id != binding.account_id or binding.account_id != link.account_id:
        raise AuthorizationFailure("invalid_token", "The account binding is inconsistent")
    if binding.resource != link.resource:
        raise AuthorizationFailure(
            "invalid_target", "The resource audience does not exactly match", status=403
        )
    if token.resource != [binding.resource]:
        raise AuthorizationFailure(
            "invalid_target", "The token resource indicator does not exactly match", status=403
        )
    token_scopes = frozenset(filter(None, token.scope.split()))
    return MCPPrincipal(
        account_id=binding.account_id,
        oauth_grant_id=binding.oauth_grant_id,
        access_token_id=token.pk,
        device_id=link.device_id,
        device_link_id=link.pk,
        link_id=link.link_id,
        resource=link.resource,
        scopes=token_scopes,
    )


async def authenticate_mcp(headers: Mapping[str, str], link_id_text: str) -> MCPPrincipal:
    try:
        link_id = UUID(link_id_text)
    except ValueError as exc:
        raise AuthorizationFailure("invalid_target", "Malformed device link", status=404) from exc
    raw_token = _bearer(headers)
    return await database_sync(_authenticate, raw_token, link_id)
