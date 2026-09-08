from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from codito_relay.core.authz import AuthorizationFailure, authenticate_mcp
from codito_relay.core.models import AuditEvent, DeviceLink, Invitation


@pytest.mark.django_db
def test_invitation_is_single_use_and_email_bound(user) -> None:  # type: ignore[no-untyped-def]
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    invitation, raw = Invitation.issue(email="new@example.test", created_by=user)
    client = Client()
    response = client.post(
        f"/invite/{raw}/",
        {
            "email": "attacker@example.test",
            "username": "new-person",
            "password1": "a-long-and-unique-password-97531",
            "password2": "a-long-and-unique-password-97531",
        },
        secure=True,
    )
    assert response.status_code == 302
    invitation.refresh_from_db()
    assert invitation.used_at is not None
    created = get_user_model().objects.get(username="new-person")
    assert created.email == "new@example.test"
    assert client.get(f"/invite/{raw}/", secure=True).status_code == 404


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_mcp_authentication_binds_exact_link(oauth_token, link, device) -> None:  # type: ignore[no-untyped-def]
    raw, _ = oauth_token
    principal = await authenticate_mcp({"authorization": f"Bearer {raw}"}, str(link.link_id))
    assert principal.device_id == device.pk
    other = await __import__("asgiref.sync").sync.sync_to_async(DeviceLink.objects.create)(
        account=device.account, device=device
    )
    with pytest.raises(AuthorizationFailure, match="another MCP resource"):
        await authenticate_mcp({"authorization": f"Bearer {raw}"}, str(other.link_id))


@pytest.mark.django_db
def test_audit_rejects_sensitive_payload_keys(user) -> None:  # type: ignore[no-untyped-def]
    event = AuditEvent(
        account=user,
        actor_type="user",
        actor_id=str(user.pk),
        event_type="test",
        metadata={"stdout": "secret output"},
    )
    with pytest.raises(ValidationError):
        event.full_clean()


@pytest.mark.django_db
def test_audit_rejects_nested_sensitive_payload_keys(user) -> None:  # type: ignore[no-untyped-def]
    event = AuditEvent(
        account=user,
        actor_type="user",
        actor_id=str(user.pk),
        event_type="test",
        metadata={"details": [{"request": {"access_token": "canary-secret"}}]},
    )
    with pytest.raises(ValidationError, match="access_token"):
        event.full_clean()


@pytest.mark.django_db
def test_expired_invitation_is_not_usable(user) -> None:  # type: ignore[no-untyped-def]
    invitation, _ = Invitation.issue(email="new@example.test", created_by=user)
    invitation.expires_at = timezone.now() - timedelta(seconds=1)
    invitation.save(update_fields=["expires_at"])
    assert not invitation.usable
