from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client


@pytest.mark.django_db
def test_browser_session_expires_at_absolute_limit(user, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    started = 2_000_000_000
    monkeypatch.setattr("codito_relay.core.middleware.unix_time", lambda: started)
    client = Client()
    client.force_login(user)
    assert client.get("/", secure=True).status_code == 200

    monkeypatch.setattr(
        "codito_relay.core.middleware.unix_time",
        lambda: started + settings.SESSION_ABSOLUTE_TIMEOUT_SECONDS,
    )
    response = client.get("/", secure=True)
    assert response.status_code == 302
    assert response.url.startswith(settings.LOGIN_URL)
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_browser_session_expires_at_idle_limit(user, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    started = 2_000_000_000
    monkeypatch.setattr("codito_relay.core.middleware.unix_time", lambda: started)
    client = Client()
    client.force_login(user)
    assert client.get("/", secure=True).status_code == 200

    monkeypatch.setattr(
        "codito_relay.core.middleware.unix_time",
        lambda: started + settings.SESSION_IDLE_TIMEOUT_SECONDS,
    )
    response = client.get("/", secure=True)
    assert response.status_code == 302
    assert response.url.startswith(settings.LOGIN_URL)
    assert "_auth_user_id" not in client.session
