from __future__ import annotations

import platform
from dataclasses import dataclass

from .config import AgentConfig
from .credentials import DeviceCredentialStore, DeviceIdentity
from .db import AgentDatabase
from .errors import AgentError
from .oidc import DesktopOidcLogin
from .relay_client import RelayClient, RelayTokenState, RelayTokenStore


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    identity: DeviceIdentity
    tokens: RelayTokenState


def _services(
    config: AgentConfig,
) -> tuple[DeviceCredentialStore, AgentDatabase, RelayTokenStore, RelayClient]:
    config.ensure_directories()
    credentials = DeviceCredentialStore(config.data_directory / "credentials")
    database = AgentDatabase(config.data_directory / "agent.sqlite3")
    tokens = RelayTokenStore(config.data_directory / "tokens.dpapi")
    relay = RelayClient(config.relay_http_url, config.desktop_client_id, tokens, credentials)
    return credentials, database, tokens, relay


async def sign_in(config: AgentConfig, *, reenroll: bool = False) -> EnrollmentResult:
    """Complete browser PKCE login and create or resume the device enrollment."""

    credentials, database, _tokens, relay = _services(config)
    login = DesktopOidcLogin(
        f"{config.relay_http_url.rstrip('/')}/o/authorize/",
        config.desktop_client_id,
        f"{config.relay_http_url.rstrip('/')}/device-api",
    )
    authorization = await login.authorize()
    state = await relay.exchange_code(authorization)
    if reenroll and credentials.metadata_path.exists():
        database.revoke_read_permissions()
        database.revoke_shell_permissions()
        database.revoke_screen_permissions()
        identity = credentials.rotate()
        database.rotate_project_ids()
    elif credentials.metadata_path.exists():
        identity = credentials.load()
    else:
        identity = credentials.enroll()
    if identity.device_id.startswith("local_"):
        enrolled = await relay.enroll_device(state, platform.node() or "Windows device")
    else:
        try:
            enrolled = await relay.resume_device(state, identity.device_id)
        except AgentError as exc:
            if exc.code != "enrollment_required":
                raise
            raise AgentError(
                "reenrollment_required",
                "This device was revoked. Choose Re-enroll device to rotate its key and MCP link.",
            ) from exc
    return EnrollmentResult(credentials.load(), enrolled)


async def sign_out(config: AgentConfig) -> None:
    """Revoke the relay device, clear OAuth tokens, and stage a fresh local key."""

    credentials, database, tokens, relay = _services(config)
    state = tokens.load()
    try:
        state = await relay.ensure_access_token()
        if state.device_id is not None:
            await relay.revoke_device(state, state.device_id)
    finally:
        database.revoke_read_permissions()
        database.revoke_shell_permissions()
        database.revoke_screen_permissions()
        tokens.clear()
    if credentials.metadata_path.exists():
        credentials.rotate()
        database.rotate_project_ids()
