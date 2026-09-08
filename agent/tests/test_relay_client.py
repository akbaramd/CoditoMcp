from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codito_agent.errors import AgentError
from codito_agent.relay_client import RelayClient, RelayTokenState, RelayTokenStore


class ReversibleProtector:
    def protect(self, value: bytes) -> bytes:
        return b"test:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        return value[5:][::-1]


@pytest.mark.asyncio
async def test_ticket_atomically_resynchronizes_rotated_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RelayTokenStore(tmp_path / "tokens", protector=ReversibleProtector())
    state = RelayTokenState(
        access_token="access",  # noqa: S106 - inert test value
        refresh_token="refresh",  # noqa: S106 - inert test value
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        account_id="account_abcdefghijkl",
        device_id="device_abcdefghijkl",
        link_id="link_old_abcdefghijk",
        mcp_url="https://example.test/mcp/d/link_old_abcdefghijk",
    )
    store.save(state)
    client = RelayClient("https://example.test", "desktop-client", store, object())  # type: ignore[arg-type]

    async def ticket_response(
        token_state: RelayTokenState, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        assert token_state.device_id == state.device_id
        assert path.endswith("/tickets/")
        assert payload == {}
        return {
            "ticket": "ticket_abcdefghijkl",
            "challenge": "challenge_abcdefghijkl",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            "link_id": "link_new_abcdefghijk",
            "mcp_url": "https://example.test/mcp/d/link_new_abcdefghijk",
        }

    monkeypatch.setattr(client, "_authorized_post", ticket_response)
    ticket = await client.issue_websocket_ticket()
    persisted = store.load()
    assert ticket.link_id == "link_new_abcdefghijk"
    assert persisted.link_id == ticket.link_id
    assert persisted.mcp_url == "https://example.test/mcp/d/link_new_abcdefghijk"


@pytest.mark.asyncio
async def test_relogin_resumes_bound_device_without_duplicate_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RelayTokenStore(tmp_path / "tokens", protector=ReversibleProtector())
    state = RelayTokenState(
        access_token="access",  # noqa: S106 - inert test value
        refresh_token="refresh",  # noqa: S106 - inert test value
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        account_id="account_abcdefghijkl",
    )
    device_id = "device_abcdefghijkl"

    class Credentials:
        def load(self) -> Any:
            return SimpleNamespace(device_id=device_id)

    client = RelayClient("https://example.test", "desktop-client", store, Credentials())  # type: ignore[arg-type]

    async def devices(token_state: RelayTokenState, path: str) -> dict[str, Any]:
        assert token_state is state
        assert path == "/api/devices/"
        return {
            "devices": [
                {
                    "device_id": device_id,
                    "link_id": "link_resumed1234567",
                    "status": "offline",
                }
            ]
        }

    monkeypatch.setattr(client, "_authorized_get", devices)
    resumed = await client.resume_device(state, device_id)
    assert resumed.device_id == device_id
    assert resumed.link_id == "link_resumed1234567"
    assert resumed.mcp_url == "https://example.test/mcp/d/link_resumed1234567"
    assert store.load() == resumed


@pytest.mark.asyncio
async def test_relogin_fails_closed_when_device_belongs_to_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RelayTokenStore(tmp_path / "tokens", protector=ReversibleProtector())
    state = RelayTokenState(
        access_token="access",  # noqa: S106 - inert test value
        refresh_token="refresh",  # noqa: S106 - inert test value
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        account_id="account_abcdefghijkl",
    )
    device_id = "device_abcdefghijkl"

    class Credentials:
        def load(self) -> Any:
            return SimpleNamespace(device_id=device_id)

    client = RelayClient("https://example.test", "desktop-client", store, Credentials())  # type: ignore[arg-type]

    async def no_devices(token_state: RelayTokenState, path: str) -> dict[str, Any]:
        return {"devices": []}

    monkeypatch.setattr(client, "_authorized_get", no_devices)
    with pytest.raises(AgentError) as error:
        await client.resume_device(state, device_id)
    assert error.value.code == "enrollment_required"
