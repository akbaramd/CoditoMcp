from __future__ import annotations

import ipaddress
import json
import secrets
import time
from base64 import urlsafe_b64decode
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.http.request import validate_host
from jwcrypto import jwk, jws  # type: ignore[import-untyped]
from jwcrypto.common import JWException  # type: ignore[import-untyped]
from oauth2_provider.cimd import CIMDError, SafeMetadataFetcher
from oauth2_provider.models import Application
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc6749 import errors  # type: ignore[import-untyped]
from redis.exceptions import RedisError

from .diagnostics import emit, scope_summary


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

        try:
            raw_metadata, raw_max_age = super().fetch(client_id)
        except CIMDError:
            emit("oauth.cimd_rejected", reason="metadata_fetch")
            raise
        emit("oauth.cimd_fetched")
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

    @staticmethod
    def _has_private_key_assertion(request: Any) -> bool:
        return bool(
            getattr(request, "client_assertion", None)
            or getattr(request, "client_assertion_type", None)
        )

    def client_authentication_required(self, request: Any, *args: Any, **kwargs: Any) -> bool:
        if self._has_private_key_assertion(request):
            return True
        return bool(super().client_authentication_required(request, *args, **kwargs))

    def authenticate_client(self, request: Any, *args: Any, **kwargs: Any) -> bool:
        if self._has_private_key_assertion(request):
            return self._authenticate_private_key_jwt(request)
        return bool(super().authenticate_client(request, *args, **kwargs))

    @staticmethod
    def _decode_jwt_part(value: str) -> dict[str, Any]:
        if len(value) > 8192:
            raise ValueError("JWT section is too large")
        padded = value + "=" * (-len(value) % 4)
        decoded = urlsafe_b64decode(padded.encode("ascii"))
        parsed = json.loads(decoded)
        if not isinstance(parsed, dict):
            raise ValueError("JWT section is not an object")
        return parsed

    @staticmethod
    def _approved_https_url(value: object) -> str:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError("URL is invalid")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or not validate_host(parsed.hostname, settings.CHATGPT_CLIENT_METADATA_HOSTS)
        ):
            raise ValueError("URL host is not approved")
        return value

    def _authenticate_private_key_jwt(self, request: Any) -> bool:
        """Authenticate ChatGPT CIMD clients using RFC 7523 assertions.

        DOT 3.4 represents CIMD clients as public applications but does not yet
        validate ``private_key_jwt``. Codito therefore verifies the assertion
        against the client's live, SSRF-hardened metadata and JWKS documents.
        """

        stage = "request"

        def rejected() -> bool:
            emit("oauth.assertion_rejected", stage=stage)
            return False

        try:
            assertion = getattr(request, "client_assertion", None)
            assertion_type = getattr(request, "client_assertion_type", None)
            client_id = getattr(request, "client_id", None)
            if (
                assertion_type != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                or not isinstance(assertion, str)
                or len(assertion) > 16_384
                or getattr(request, "client_secret", None)
                or self._extract_basic_auth(request)
            ):
                return rejected()

            stage = "header"
            parts = assertion.split(".")
            if len(parts) != 3:
                return rejected()
            header = self._decode_jwt_part(parts[0])
            unverified_claims = self._decode_jwt_part(parts[1])
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                return rejected()
            stage = "issuer"
            issuer = self._approved_https_url(unverified_claims.get("iss"))
            if unverified_claims.get("sub") != issuer or (client_id and client_id != issuer):
                return rejected()

            stage = "client_registration"
            client = self._load_application(issuer, request)
            if not is_allowed_mcp_application(client):
                return rejected()

            stage = "metadata_fetch"
            metadata, _ = SafeMetadataFetcher().fetch(issuer)
            stage = "metadata_policy"
            supported = metadata.get("token_endpoint_auth_methods_supported", [])
            if (
                metadata.get("client_id") != issuer
                or metadata.get("token_endpoint_auth_method") != "private_key_jwt"
                or not isinstance(supported, list)
                or "private_key_jwt" not in supported
                or metadata.get("token_endpoint_auth_signing_alg") != "RS256"
            ):
                return rejected()
            stage = "jwks_fetch"
            jwks_uri = self._approved_https_url(metadata.get("jwks_uri"))
            jwks_document, _ = SafeMetadataFetcher().fetch(jwks_uri)
            stage = "key_selection"
            keys = jwks_document.get("keys")
            if not isinstance(keys, list):
                return rejected()
            public_keys = [
                value
                for value in keys
                if isinstance(value, dict)
                and value.get("kid") == header["kid"]
                and value.get("kty") == "RSA"
                and "d" not in value
            ]
            if len(public_keys) != 1:
                return rejected()

            stage = "signature"
            verifier = jws.JWS()
            verifier.deserialize(assertion)
            verifier.verify(jwk.JWK(**public_keys[0]), alg="RS256")
            claims = json.loads(verifier.payload)
            if not isinstance(claims, dict) or claims != unverified_claims:
                return rejected()

            stage = "claims_time_audience"
            now = int(time.time())
            exp = claims.get("exp")
            issued_at = claims.get("iat")
            not_before = claims.get("nbf", issued_at)
            jti = claims.get("jti")
            audiences = claims.get("aud")
            audience_values = [audiences] if isinstance(audiences, str) else audiences
            valid_audiences = {
                settings.PUBLIC_BASE_URL,
                f"{settings.PUBLIC_BASE_URL}/o/token/",
            }
            if (
                not isinstance(exp, int)
                or not isinstance(issued_at, int)
                or not isinstance(not_before, int)
                or exp <= now - 60
                or exp > now + 300
                or issued_at > now + 60
                or not_before > now + 60
                or exp <= issued_at
                or not isinstance(jti, str)
                or not (8 <= len(jti) <= 200)
                or not isinstance(audience_values, list)
                or not audience_values
                or any(not isinstance(value, str) for value in audience_values)
                or not any(
                    secrets.compare_digest(value, allowed)
                    for value in audience_values
                    for allowed in valid_audiences
                )
            ):
                return rejected()

            stage = "replay_protection"
            replay_key = f"codito:oauth:private-key-jwt:{sha256(jti.encode()).hexdigest()}"
            if not cache.add(replay_key, True, timeout=max(1, exp - now + 60)):
                return rejected()
            request.client = client
            request.client_id = issuer
            emit("oauth.assertion_accepted")
            return True
        except (
            CIMDError,
            JWException,
            KeyError,
            RedisError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return rejected()

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
            emit("oauth.scopes_rejected", reason="provider_policy", **scope_summary(scopes))
            return False
        if self._is_desktop(client):
            allowed = {"openid", "profile", "device:manage"}
        elif is_allowed_mcp_application(client):
            allowed = set(settings.MCP_TOOL_SCOPES)
        else:
            allowed = set()
        valid = set(scopes).issubset(allowed)
        if not valid:
            emit("oauth.scopes_rejected", reason="client_policy", **scope_summary(scopes))
        return valid

    def validate_redirect_uri(
        self,
        client_id: str,
        redirect_uri: str,
        request: Any,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        if not super().validate_redirect_uri(client_id, redirect_uri, request, *args, **kwargs):
            emit("oauth.redirect_rejected", reason="registered_uri_mismatch")
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
            emit("oauth.resource_rejected", reason="resource_count", resource_count=len(resources))
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
            emit("oauth.resource_rejected", reason="client_resource_policy")
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
