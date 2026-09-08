from __future__ import annotations

import base64
import hashlib
import json
import time
from io import StringIO
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import Client
from jwcrypto import jwk, jwt
from oauth2_provider.cimd import SafeMetadataFetcher
from oauth2_provider.models import AccessToken, Application

from codito_relay.core.models import OAuthGrantBinding, OAuthGrantFamily


def pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


@pytest.mark.django_db(transaction=True)
def test_desktop_pkce_token_userinfo_enrollment_and_ticket(user) -> None:  # type: ignore[no-untyped-def]
    redirect_uri = "http://127.0.0.1:49157/callback"
    registered_redirect = "http://127.0.0.1:8765/callback"
    application = Application.objects.create(
        client_id=settings.DESKTOP_OAUTH_CLIENT_ID,
        name="Codito Windows Agent",
        user=user,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=registered_redirect,
        client_secret="",
        hash_client_secret=False,
        algorithm=Application.RS256_ALGORITHM,
    )
    verifier = "pkce-verifier-0123456789abcdefghijklmnopqrstuvwxyz-ABCDE"
    authorize = {
        "client_id": application.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid profile device:manage",
        "resource": settings.DESKTOP_RESOURCE,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "state": "desktop-state-0123456789",
    }
    client = Client()
    client.force_login(user)
    consent = client.get(f"/o/authorize/?{urlencode(authorize)}", secure=True)
    assert consent.status_code == 200, consent.content.decode()
    approved = client.post(
        "/o/authorize/",
        {
            **authorize,
            "allow": "Authorize",
        },
        secure=True,
    )
    assert approved.status_code == 302, approved.content.decode()
    callback = parse_qs(urlparse(approved["Location"]).query)
    assert callback["state"] == [authorize["state"]]
    code = callback["code"][0]
    token_response = client.post(
        "/o/token/",
        {
            "grant_type": "authorization_code",
            "client_id": application.client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": settings.DESKTOP_RESOURCE,
        },
        secure=True,
    )
    assert token_response.status_code == 200, token_response.content.decode()
    raw_token = token_response.json()["access_token"]
    checksum = hashlib.sha256(raw_token.encode()).hexdigest()
    token = AccessToken.objects.get(token_checksum=checksum)
    assert token.resource == [settings.DESKTOP_RESOURCE]
    assert not hasattr(token, "codito_binding")

    bearer = {"HTTP_AUTHORIZATION": f"Bearer {raw_token}"}
    userinfo = client.get("/oidc/userinfo", secure=True, **bearer)
    assert userinfo.status_code == 200
    assert userinfo.json()["sub"] == str(user.pk)
    enrolled = client.post(
        "/api/devices/enroll/",
        data=json.dumps(
            {
                "name": "Windows test device",
                "public_key_jwk": {"kty": "EC", "crv": "P-256", "x": "x", "y": "y"},
                "key_thumbprint": "9" * 64,
            }
        ),
        content_type="application/json",
        secure=True,
        **bearer,
    )
    assert enrolled.status_code == 201, enrolled.content.decode()
    device_id = enrolled.json()["device_id"]
    ticket = client.post(f"/api/devices/{device_id}/tickets/", secure=True, **bearer)
    assert ticket.status_code == 200
    assert ticket.json()["ticket"]
    assert ticket.json()["link_id"] == enrolled.json()["link_id"]
    assert ticket.json()["mcp_url"] == enrolled.json()["mcp_url"]

    duplicate_name = client.post(
        "/api/devices/enroll/",
        data=json.dumps(
            {
                "name": "Windows test device",
                "public_key_jwk": {"kty": "EC", "crv": "P-256", "x": "x2", "y": "y2"},
                "key_thumbprint": "8" * 64,
            }
        ),
        content_type="application/json",
        secure=True,
        **bearer,
    )
    assert duplicate_name.status_code == 409
    assert duplicate_name.json()["error"] == "device_name_conflict"

    revoked = client.post(f"/api/devices/{device_id}/revoke/", secure=True, **bearer)
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert (
        client.post(f"/api/devices/{device_id}/tickets/", secure=True, **bearer).status_code == 404
    )
    reenrolled = client.post(
        "/api/devices/enroll/",
        data=json.dumps(
            {
                "name": "Windows test device",
                "public_key_jwk": {"kty": "EC", "crv": "P-256", "x": "x2", "y": "y2"},
                "key_thumbprint": "8" * 64,
            }
        ),
        content_type="application/json",
        secure=True,
        **bearer,
    )
    assert reenrolled.status_code == 201
    assert reenrolled.json()["device_id"] != device_id


@pytest.mark.django_db
def test_device_api_rejects_cookie_session(user) -> None:  # type: ignore[no-untyped-def]
    client = Client()
    client.force_login(user)
    response = client.post(
        "/api/devices/enroll/",
        data="{}",
        content_type="application/json",
        secure=True,
    )
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_mcp_pkce_exchange_creates_exact_resource_binding(user, link) -> None:  # type: ignore[no-untyped-def]
    redirect_uri = "https://chatgpt.com/aip/plugin/oauth/callback"
    application = Application.objects.create(
        client_id="https://chatgpt.com/.well-known/oauth-client/codito-test",
        name="ChatGPT MCP test client",
        user=None,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=redirect_uri,
        client_secret="",
        hash_client_secret=False,
        registration_source=Application.RegistrationSource.CIMD,
    )
    verifier = "mcp-pkce-verifier-0123456789abcdefghijklmnopqrstuvwxyz-ABCDE"
    authorize = {
        "client_id": application.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "projects:read files:read",
        "resource": link.resource,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "state": "mcp-state-0123456789",
    }
    client = Client()
    client.force_login(user)
    consent = client.get(f"/o/authorize/?{urlencode(authorize)}", secure=True)
    assert consent.status_code == 200, consent.content.decode()
    approved = client.post(
        "/o/authorize/",
        {**authorize, "allow": "Authorize"},
        secure=True,
    )
    assert approved.status_code == 302, approved.content.decode()
    code = parse_qs(urlparse(approved["Location"]).query)["code"][0]

    token_response = client.post(
        "/o/token/",
        {
            "grant_type": "authorization_code",
            "client_id": application.client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": link.resource,
        },
        secure=True,
    )
    assert token_response.status_code == 200, token_response.content.decode()
    raw_token = token_response.json()["access_token"]
    token = AccessToken.objects.get(token_checksum=hashlib.sha256(raw_token.encode()).hexdigest())
    binding = OAuthGrantBinding.objects.get(access_token=token)
    assert token.resource == [link.resource]
    assert binding.resource == link.resource
    assert binding.account_id == user.pk
    assert binding.device_link_id == link.pk
    assert binding.oauth_grant_id
    assert frozenset(token.scope.split()) == frozenset({"projects:read", "files:read"})

    original_grant_id = binding.oauth_grant_id
    refresh_response = client.post(
        "/o/token/",
        {
            "grant_type": "refresh_token",
            "client_id": application.client_id,
            "refresh_token": token_response.json()["refresh_token"],
        },
        secure=True,
    )
    assert refresh_response.status_code == 200, refresh_response.content.decode()
    refreshed_raw = refresh_response.json()["access_token"]
    refreshed = AccessToken.objects.get(
        token_checksum=hashlib.sha256(refreshed_raw.encode()).hexdigest()
    )
    refreshed_binding = OAuthGrantBinding.objects.get(access_token=refreshed)
    assert refreshed.resource == [link.resource]
    assert refreshed_binding.oauth_grant_id == original_grant_id
    assert OAuthGrantFamily.objects.get().oauth_grant_id == original_grant_id


@pytest.mark.django_db(transaction=True)
def test_chatgpt_private_key_jwt_exchange_and_replay_protection(  # type: ignore[no-untyped-def]
    user, link, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_id = "https://chatgpt.com/oauth/codito-test/client.json"
    redirect_uri = "https://chatgpt.com/connector/oauth/codito-test"
    jwks_uri = "https://chatgpt.com/oauth/jwks.json"
    application = Application.objects.create(
        client_id=client_id,
        name="ChatGPT signed MCP test client",
        user=None,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=redirect_uri,
        client_secret="",
        hash_client_secret=False,
        registration_source=Application.RegistrationSource.CIMD,
    )
    signing_key = jwk.JWK.generate(kty="RSA", size=2048)
    signing_key.update(kid="chatgpt-test-key")
    public_key = json.loads(signing_key.export_public())
    metadata = {
        "client_id": client_id,
        "token_endpoint_auth_method": "private_key_jwt",
        "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
        "token_endpoint_auth_signing_alg": "RS256",
        "jwks_uri": jwks_uri,
    }

    def fake_fetch(_self: SafeMetadataFetcher, url: str):  # type: ignore[no-untyped-def]
        if url == client_id:
            return metadata, 300
        if url == jwks_uri:
            return {"keys": [public_key]}, 300
        raise AssertionError(f"unexpected metadata URL: {url}")

    monkeypatch.setattr(SafeMetadataFetcher, "fetch", fake_fetch)
    verifier = "signed-pkce-verifier-0123456789abcdefghijklmnopqrstuvwxyz-ABCDE"
    authorize = {
        "client_id": application.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "projects:read files:read",
        "resource": link.resource,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "state": "signed-state-0123456789",
    }
    client = Client()
    client.force_login(user)

    def authorize_code() -> str:
        consent = client.get(f"/o/authorize/?{urlencode(authorize)}", secure=True)
        assert consent.status_code == 200, consent.content.decode()
        approved = client.post("/o/authorize/", {**authorize, "allow": "Authorize"}, secure=True)
        assert approved.status_code == 302, approved.content.decode()
        return parse_qs(urlparse(approved["Location"]).query)["code"][0]

    now = int(time.time())
    claims = {
        "iss": client_id,
        "sub": client_id,
        "aud": f"{settings.PUBLIC_BASE_URL}/o/token/",
        "iat": now,
        "nbf": now,
        "exp": now + 180,
        "jti": str(uuid4()),
    }
    assertion = jwt.JWT(header={"alg": "RS256", "kid": "chatgpt-test-key"}, claims=claims)
    assertion.make_signed_token(signing_key)
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "resource": link.resource,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion.serialize(),
    }
    accepted = client.post("/o/token/", {**form, "code": authorize_code()}, secure=True)
    assert accepted.status_code == 200, accepted.content.decode()
    token = AccessToken.objects.get(
        token_checksum=hashlib.sha256(accepted.json()["access_token"].encode()).hexdigest()
    )
    assert OAuthGrantBinding.objects.get(access_token=token).resource == link.resource

    replayed = client.post("/o/token/", {**form, "code": authorize_code()}, secure=True)
    assert replayed.status_code == 401
    assert replayed.json()["error"] == "invalid_client"


@pytest.mark.django_db(transaction=True)
def test_manual_public_client_cannot_receive_mcp_grant(user, link) -> None:  # type: ignore[no-untyped-def]
    redirect_uri = "https://chatgpt.com/aip/plugin/oauth/callback"
    application = Application.objects.create(
        client_id="manual-client-not-cimd",
        name="Untrusted manual client",
        user=user,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=redirect_uri,
        client_secret="",
        hash_client_secret=False,
    )
    query = {
        "client_id": application.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "projects:read files:read",
        "resource": link.resource,
        "code_challenge": pkce_challenge(
            "manual-client-verifier-0123456789abcdefghijklmnopqrstuvwxyz"
        ),
        "code_challenge_method": "S256",
        "state": "manual-client-state-0123456789",
    }
    client = Client()
    client.force_login(user)
    response = client.get(f"/o/authorize/?{urlencode(query)}", secure=True)
    assert response.status_code in {302, 400}
    if response.status_code == 302:
        assert parse_qs(urlparse(response["Location"]).query)["error"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scope", "resource"),
    [
        ("projects:read files:read", "mcp"),
        ("openid profile device:manage", "mcp"),
    ],
)
def test_desktop_client_cannot_request_mcp_scope_or_resource(  # type: ignore[no-untyped-def]
    user, link, scope: str, resource: str
) -> None:
    application = Application.objects.create(
        client_id=settings.DESKTOP_OAUTH_CLIENT_ID,
        name="Codito Windows Agent",
        user=user,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="http://127.0.0.1:8765/callback",
        client_secret="",
        hash_client_secret=False,
        algorithm=Application.RS256_ALGORITHM,
    )
    verifier = "negative-pkce-verifier-0123456789abcdefghijklmnopqrstuvwxyz"
    query = {
        "client_id": application.client_id,
        "response_type": "code",
        "redirect_uri": "http://127.0.0.1:49157/callback",
        "scope": scope,
        "resource": link.resource if resource == "mcp" else settings.DESKTOP_RESOURCE,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "state": "negative-state-0123456789",
    }
    client = Client()
    client.force_login(user)
    response = client.get(f"/o/authorize/?{urlencode(query)}", secure=True)
    if response.status_code == 200:
        response = client.post(
            "/o/authorize/",
            {**query, "allow": "Authorize"},
            secure=True,
        )
    assert response.status_code in {302, 400}
    if response.status_code == 302:
        assert parse_qs(urlparse(response["Location"]).query)["error"]


@pytest.mark.django_db
def test_desktop_client_provisioning_is_deterministic() -> None:
    output = StringIO()
    call_command("provision_desktop_client", stdout=output)
    call_command("provision_desktop_client", stdout=output)
    application = Application.objects.get(client_id="codito-windows-agent")
    assert application.name == "Codito Windows Agent"
    assert application.client_type == Application.CLIENT_PUBLIC
    assert application.algorithm == Application.RS256_ALGORITHM
