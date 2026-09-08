from __future__ import annotations

import ipaddress
import secrets
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.http.request import validate_host
from oauth2_provider.cimd import CIMDError, SafeMetadataFetcher
from oauth2_provider.models import Application
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc6749 import errors  # type: ignore[import-untyped]


def exact_resource_match(request_uri: str, audiences: list[str]) -> bool:
    """Require the one per-device resource audience to match the request exactly."""

    return len(audiences) == 1 and secrets.compare_digest(request_uri, audiences[0])


def is_allowed_mcp_application(client: Application | None) -> bool:
    """Accept only public, authorization-code CIMD clients from approved hosts."""

    if (
        client is None
        or client.registration_source != Application.RegistrationSource.CIMD
        or client.client_type != Application.CLIENT_PUBLIC
        or client.authorization_grant_type != Application.GRANT_AUTHORIZATION_CODE
    ):
        return False
    parsed = urlsplit(client.client_id)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and validate_host(parsed.hostname, settings.CHATGPT_CLIENT_METADATA_HOSTS)
    )


class CoditoCIMDMetadataFetcher(SafeMetadataFetcher):  # type: ignore[misc]
    """Adapt ChatGPT's preferred private-key method to its explicit public fallback.

    Network retrieval stays entirely in DOT's ``SafeMetadataFetcher``. The
    only accepted adaptation is ``private_key_jwt`` to ``none`` when an
    approved host explicitly declares ``none`` in its supported-method array.
    """

    def fetch(self, client_id: str) -> tuple[dict[str, Any], int]:
        host = urlsplit(client_id).hostname
        if not host or not validate_host(host, settings.CHATGPT_CLIENT_METADATA_HOSTS):
            raise CIMDError("client metadata host is not approved by Codito")

        raw_metadata, raw_max_age = super().fetch(client_id)
        metadata = dict(raw_metadata)
        method = metadata.get("token_endpoint_auth_method")
        supported = metadata.get("token_endpoint_auth_methods_supported")
        explicitly_supports_none = (
            isinstance(supported, list)
            and all(isinstance(value, str) for value in supported)
            and "none" in supported
        )
        if method == "none":
            return metadata, int(raw_max_age)
        if method == "private_key_jwt" and explicitly_supports_none:
            metadata["token_endpoint_auth_method"] = "none"  # noqa: S105 - OAuth enum.
            return metadata, int(raw_max_age)
        raise CIMDError("client metadata does not explicitly select or support public auth")


class CoditoOAuth2Validator(OAuth2Validator):  # type: ignore[misc]
    """Keep desktop identity grants disjoint from per-device MCP grants."""

    @staticmethod
    def _is_desktop(client: Application | None) -> bool:
        return bool(client and client.client_id == settings.DESKTOP_OAUTH_CLIENT_ID)

    def validate_scopes(
        self,
        client_id: str,
        scopes: list[str],
        client: Application,
        request: Any,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        if not super().validate_scopes(client_id, scopes, client, request, *args, **kwargs):
            return False
        if self._is_desktop(client):
            allowed = {"openid", "profile", "device:manage"}
        elif is_allowed_mcp_application(client):
            allowed = set(settings.MCP_TOOL_SCOPES)
        else:
            allowed = set()
        return set(scopes).issubset(allowed)

    def validate_redirect_uri(
        self,
        client_id: str,
        redirect_uri: str,
        request: Any,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        if not super().validate_redirect_uri(client_id, redirect_uri, request, *args, **kwargs):
            return False
        parsed = urlsplit(redirect_uri)
        if parsed.scheme != "http":
            return True
        try:
            is_loopback = (
                bool(parsed.hostname) and ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            is_loopback = False
        return self._is_desktop(getattr(request, "client", None)) and is_loopback

    def _enforce_client_resource(self, request: Any, resources: list[str]) -> None:
        client = getattr(request, "client", None)
        if len(resources) != 1:
            raise errors.CustomOAuth2Error(
                error="invalid_target",
                description="Codito grants require exactly one resource indicator",
                request=request,
            )
        resource = resources[0]
        if self._is_desktop(client):
            valid = secrets.compare_digest(resource, settings.DESKTOP_RESOURCE)
        elif is_allowed_mcp_application(client):
            from .models import DeviceLink

            try:
                link_id = UUID(resource.rstrip("/").rsplit("/", 1)[-1])
            except ValueError:
                valid = False
            else:
                valid = resource.startswith(f"{settings.PUBLIC_BASE_URL}/mcp/d/") and (
                    DeviceLink.objects.filter(
                        link_id=link_id,
                        revoked_at__isnull=True,
                        device__revoked_at__isnull=True,
                    ).exists()
                )
        else:
            valid = False
        if not valid:
            raise errors.CustomOAuth2Error(
                error="invalid_target",
                description="The OAuth client class is not allowed to use this resource",
                request=request,
            )

    def _validate_resource_uris(self, request: Any, resources: list[str]) -> None:
        super()._validate_resource_uris(request, resources)
        # DOT validates the initially empty token-request value before it
        # inherits the resource from an authorization code or refresh token.
        if resources:
            self._enforce_client_resource(request, resources)

    def _check_and_set_request_resource(self, request: Any) -> None:
        super()._check_and_set_request_resource(request)
        self._enforce_client_resource(request, list(request.resource or []))

    def save_authorization_code(
        self,
        client_id: str,
        code: dict[str, str],
        request: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        resource = getattr(request, "resource", [])
        resources = [resource] if isinstance(resource, str) else list(resource or [])
        request.resource = resources
        self._validate_resource_uris(request, resources)
        super().save_authorization_code(client_id, code, request, *args, **kwargs)
