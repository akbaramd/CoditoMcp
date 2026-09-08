from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, RefreshToken
from oauth2_provider.oauth2_validators import OAuth2Validator
from test_desktop_oauth import HiddenConsentFields, pkce_challenge

from codito_relay.core.models import OAuthGrantBinding, OAuthGrantFamily
from codito_relay.settings import env_seconds


@pytest.fixture
def oauth_session(user, link):  # type: ignore[no-untyped-def]
    redirect = "https://chatgpt.com/aip/plugin/oauth/callback"
    application = Application.objects.create(
        client_id="https://chatgpt.com/.well-known/oauth-client/refresh-test",
        name="Refresh lifetime test client",
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=redirect,
        client_secret="",
        hash_client_secret=False,
        registration_source=Application.RegistrationSource.CIMD,
    )
    verifier = "refresh-test-verifier-0123456789abcdefghijklmnopqrstuvwxyz-ABC"
    client = Client()
    client.force_login(user)
    consent = client.get(
        "/o/authorize/?"
        + urlencode(
            {
                "client_id": application.client_id,
                "response_type": "code",
                "redirect_uri": redirect,
                "scope": "projects:read files:read screen:read",
                "resource": link.resource,
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
                "state": "refresh-test-state",
            }
        ),
        secure=True,
    )
    assert consent.status_code == 200
    approved = client.post(
        "/o/authorize/",
        {**HiddenConsentFields(consent.content.decode()).fields, "allow": "Authorize"},
        secure=True,
    )
    assert approved.status_code == 302
    code = parse_qs(urlparse(approved["Location"]).query)["code"][0]
    issued = client.post(
        "/o/token/",
        {
            "grant_type": "authorization_code",
            "client_id": application.client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect,
            "resource": link.resource,
        },
        secure=True,
    )
    assert issued.status_code == 200
    return client, application, issued.json()


def refresh(client: Client, application: Application, pair: dict[str, Any]) -> Any:
    return client.post(
        "/o/token/",
        {
            "grant_type": "refresh_token",
            "client_id": application.client_id,
            "refresh_token": pair["refresh_token"],
        },
        secure=True,
    )


def access_token(pair: dict[str, Any]) -> AccessToken:
    return AccessToken.objects.get(
        token_checksum=hashlib.sha256(pair["access_token"].encode()).hexdigest()
    )


def test_bounded_oauth_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.OAUTH2_PROVIDER["ACCESS_TOKEN_EXPIRE_SECONDS"] == 3600
    assert settings.OAUTH2_PROVIDER["REFRESH_TOKEN_EXPIRE_SECONDS"] == 90 * 86400
    assert settings.OAUTH2_PROVIDER["REFRESH_TOKEN_GRACE_PERIOD_SECONDS"] == 0
    assert settings.OAUTH2_PROVIDER["REFRESH_TOKEN_REUSE_PROTECTION"] is True
    assert settings.OAUTH2_PROVIDER["COMPLIANT_BCP_RFC9700_TOKEN_STORAGE"] is True
    assert settings.SESSION_ABSOLUTE_TIMEOUT_SECONDS == 12 * 3600
    assert settings.SESSION_IDLE_TIMEOUT_SECONDS == 30 * 60
    name = "CODITO_TEST_LIFETIME_SECONDS"
    monkeypatch.delenv(name, raising=False)
    assert env_seconds(name, 3600, minimum=300, maximum=86400) == 3600
    for valid in (300, 7200, 86400):
        monkeypatch.setenv(name, str(valid))
        assert env_seconds(name, 3600, minimum=300, maximum=86400) == valid
    for invalid in ("0", "-1", "299", "86401", "forever", "3.5"):
        monkeypatch.setenv(name, invalid)
        with pytest.raises(ImproperlyConfigured):
            env_seconds(name, 3600, minimum=300, maximum=86400)


@pytest.mark.django_db(transaction=True)
def test_expired_access_rotates_without_login_and_preserves_screen_grant(  # type: ignore[no-untyped-def]
    oauth_session, link
) -> None:
    client, application, pair = oauth_session
    assert pair["expires_in"] == 3600
    original_grant = OAuthGrantBinding.objects.get(access_token=access_token(pair)).oauth_grant_id
    # The web session is deliberately absent. Token refresh does not need a
    # browser login; even the old 30-day window must no longer cut this off.
    client.cookies.clear()
    for _ in range(3):
        old = access_token(pair)
        old.expires = timezone.now() - timedelta(days=31)
        old.save(update_fields=["expires"])
        result = refresh(client, application, pair)
        assert result.status_code == 200
        rotated = result.json()
        assert rotated["access_token"] and rotated["refresh_token"]
        assert rotated["refresh_token"] != pair["refresh_token"]
        assert rotated["expires_in"] == 3600
        new = access_token(rotated)
        binding = OAuthGrantBinding.objects.get(access_token=new)
        assert new.resource == [link.resource]
        assert "screen:read" in new.scope.split()
        assert binding.oauth_grant_id == original_grant
        assert binding.device_link_id == link.pk
        assert binding.account_id == link.account_id
        assert not AccessToken.objects.filter(pk=old.pk).exists()
        assert not new.token  # Hash-only storage, including refreshed pairs.
        assert not RefreshToken.objects.get(access_token=new).token
        pair = rotated
    assert OAuthGrantFamily.objects.count() == 1
    assert OAuthGrantFamily.objects.get().oauth_grant_id == original_grant


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("seconds_past_expiry", [90 * 86400, 90 * 86400 + 1])
def test_idle_refresh_expiry_is_enforced_before_cleanup(  # type: ignore[no-untyped-def]
    oauth_session, seconds_past_expiry: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, application, pair = oauth_session
    now = timezone.now()
    token = access_token(pair)
    token.expires = now - timedelta(seconds=seconds_past_expiry)
    token.save(update_fields=["expires"])
    monkeypatch.setattr(timezone, "now", lambda: now)
    result = refresh(client, application, pair)
    assert result.status_code == 400
    assert result.json()["error"] == "invalid_grant"
    assert RefreshToken.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_later_reuse_still_revokes_entire_family(oauth_session) -> None:  # type: ignore[no-untyped-def]
    client, application, original = oauth_session
    result = refresh(client, application, original)
    assert result.status_code == 200
    successor = result.json()
    replay = refresh(client, application, original)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    assert not RefreshToken.objects.filter(revoked__isnull=True).exists()
    assert not AccessToken.objects.exists()
    assert refresh(client, application, successor).status_code == 400


@pytest.mark.django_db(transaction=True)
def test_revoke_immediately_blocks_refresh(oauth_session) -> None:  # type: ignore[no-untyped-def]
    client, application, pair = oauth_session
    revoked = client.post(
        "/o/revoke_token/",
        {
            "client_id": application.client_id,
            "token": pair["refresh_token"],
            "token_type_hint": "refresh_token",
        },
        secure=True,
    )
    assert revoked.status_code == 200
    assert refresh(client, application, pair).status_code == 400
    assert not AccessToken.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_rotation_between_validation_and_save_never_returns_empty_pair(  # type: ignore[no-untyped-def]
    oauth_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, application, original = oauth_session
    validate = OAuth2Validator.validate_refresh_token
    winner: list[dict[str, Any]] = []
    validating = False

    def interleave(self: Any, *args: Any, **kwargs: Any) -> bool:
        nonlocal validating
        valid = bool(validate(self, *args, **kwargs))
        if valid and not validating:
            validating = True
            result = refresh(client, application, original)
            assert result.status_code == 200
            winner.append(result.json())
        return valid

    with monkeypatch.context() as patch:
        patch.setattr(OAuth2Validator, "validate_refresh_token", interleave)
        loser = refresh(client, application, original)
    assert loser.status_code == 400
    assert loser.json()["error"] == "invalid_grant"
    assert loser["Cache-Control"] == "no-store"
    assert "access_token" not in loser.json()
    assert winner[0]["access_token"] and winner[0]["refresh_token"]
    assert RefreshToken.objects.filter(revoked__isnull=True).count() == 1
    assert refresh(client, application, winner[0]).status_code == 200


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_postgresql_concurrent_refresh_has_one_valid_winner(  # type: ignore[no-untyped-def]
    oauth_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks are required; exercised by the relay CI job")
    _, application, original = oauth_session
    barrier = Barrier(2, timeout=15)
    validate = OAuth2Validator.validate_refresh_token

    def synchronized(self: Any, *args: Any, **kwargs: Any) -> bool:
        valid = bool(validate(self, *args, **kwargs))
        if valid:
            barrier.wait()
        return valid

    def worker() -> tuple[int, dict[str, Any]]:
        close_old_connections()
        try:
            result = refresh(Client(), application, original)
            return result.status_code, result.json()
        finally:
            close_old_connections()

    with monkeypatch.context() as patch:
        patch.setattr(OAuth2Validator, "validate_refresh_token", synchronized)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [future.result(timeout=25) for future in futures]
    assert sorted(status for status, _ in results) == [200, 400]
    winner = next(pair for status, pair in results if status == 200)
    loser = next(pair for status, pair in results if status == 400)
    assert winner["access_token"] and winner["refresh_token"]
    assert loser["error"] == "invalid_grant"
    assert RefreshToken.objects.filter(revoked__isnull=True).count() == 1
    assert refresh(Client(), application, winner).status_code == 200
