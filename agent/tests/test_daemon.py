from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codito_agent.config import AgentConfig
from codito_agent.relay_client import RelayTokenState


def test_daemon_construction_wires_dynamic_link_without_stale_constructor_argument(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from codito_agent import daemon as daemon_module

    state = RelayTokenState(
        access_token="access",  # noqa: S106 - inert test value
        refresh_token="refresh",  # noqa: S106 - inert test value
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        account_id="account_abcdefghijkl",
        device_id="device_abcdefghijkl",
        link_id="link_abcdefghijklmnop",
        mcp_url="https://example.test/mcp/d/link_abcdefghijklmnop",
    )

    class TokenStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def load(self) -> RelayTokenState:
            return state

    class CredentialStore:
        def __init__(self, path: Path) -> None:
            self.path = path

    class SecretStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def load_or_create(self) -> bytes:
            return b"k" * 32

    class Pipe:
        def __init__(self, *args: object) -> None:
            self.args = args

    monkeypatch.setattr(daemon_module, "RelayTokenStore", TokenStore)
    monkeypatch.setattr(daemon_module, "DeviceCredentialStore", CredentialStore)
    monkeypatch.setattr(daemon_module, "IpcSecretStore", SecretStore)
    monkeypatch.setattr(daemon_module, "NamedPipeServer", Pipe)

    daemon = daemon_module.CoditoDaemon(AgentConfig(data_directory=tmp_path))
    assert daemon.shells.account_id == state.account_id
    assert daemon.adapter.device_id == state.device_id
