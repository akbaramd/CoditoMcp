from __future__ import annotations

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "codito_relay.test_settings")

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, set_token_value

from codito_relay.core.models import Device, DeviceLink, OAuthGrantBinding


@pytest.fixture
def user(db):  # type: ignore[no-untyped-def]
    return get_user_model().objects.create_user(
        username="person",
        email="person@example.test",
        password="long-test-password-123",  # noqa: S106 - non-production fixture credential.
    )


@pytest.fixture
def other_user(db):  # type: ignore[no-untyped-def]
    return get_user_model().objects.create_user(
        username="other",
        email="other@example.test",
        password="long-test-password-456",  # noqa: S106 - non-production fixture credential.
    )


@pytest.fixture
def device(user):  # type: ignore[no-untyped-def]
    return Device.objects.create(
        account=user,
        name="Workstation",
        status=Device.Status.OFFLINE,
        public_key_jwk={"kty": "EC", "crv": "P-256", "x": "unused", "y": "unused"},
        key_thumbprint="a" * 64,
    )


@pytest.fixture
def link(device):  # type: ignore[no-untyped-def]
    return DeviceLink.objects.create(account=device.account, device=device)


@pytest.fixture
def oauth_token(user, link):  # type: ignore[no-untyped-def]
    application = Application.objects.create(
        name="Test client",
        user=user,
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://chatgpt.com/aip/plugin/oauth/callback",
    )
    raw = "test-access-token-that-is-long-and-random"
    token = AccessToken(
        user=user,
        application=application,
        expires=timezone.now() + timezone.timedelta(minutes=15),
        scope="projects:read projects:write files:read files:write shell:execute",
        resource=[link.resource],
    )
    set_token_value(token, raw)
    token.save()
    OAuthGrantBinding.objects.create(
        access_token=token,
        account=user,
        device_link=link,
        resource=link.resource,
        scopes_digest="b" * 64,
        oauth_grant_id="grant_0123456789abcdef",
    )
    return raw, token
