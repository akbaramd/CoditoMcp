from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .credentials import DataProtector, DeviceCredentialStore, DpapiProtector
from .errors import AgentError
from .oidc import AuthorizationResult
from .websocket_client import WebSocketTicket


@dataclass(frozen=True, slots=True)
class RelayTokenState:
    access_token: str
    refresh_token: str
    expires_at: datetime
    account_id: str
    device_id: str | None = None
    link_id: str | None = None
    mcp_url: str | None = None


class RelayTokenStore:
    def __init__(self, path: Path, protector: DataProtector | None = None) -> None:
        self.path = path
        self.protector = protector or DpapiProtector()

    def save(self, state: RelayTokenState) -> None:
        raw = json.dumps(
            {
                "access_token": state.access_token,
                "refresh_token": state.refresh_token,
                "expires_at": state.expires_at.isoformat(),
                "account_id": state.account_id,
                "device_id": state.device_id,
                "link_id": state.link_id,
                "mcp_url": state.mcp_url,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        protected = self.protector.protect(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> RelayTokenState:
        try:
            value = json.loads(self.protector.unprotect(self.path.read_bytes()))
            expires = datetime.fromisoformat(value["expires_at"])
            if expires.tzinfo is None:
                raise ValueError("naive expiry")
            return RelayTokenState(
                access_token=value["access_token"],
                refresh_token=value["refresh_token"],
                expires_at=expires.astimezone(UTC),
                account_id=value["account_id"],
                device_id=value.get("device_id"),
                link_id=value.get("link_id"),
                mcp_url=value.get("mcp_url"),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentError("login_required", "Desktop login credentials are unavailable") from exc

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class RelayClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        token_store: RelayTokenStore,
        credentials: DeviceCredentialStore,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.token_store = token_store
        self.credentials = credentials

    async def exchange_code(self, authorization: AuthorizationResult) -> RelayTokenState:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20) as client:
            response = await client.post(
                "/o/token/",
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "code": authorization.code,
                    "code_verifier": authorization.verifier,
                    "redirect_uri": authorization.redirect_uri,
                    "resource": f"{self.base_url}/device-api",
                },
                headers={"Accept": "application/json"},
            )
            value = self._json_response(response, "OAuth code exchange failed")
            access = self._required(value, "access_token")
            refresh = self._required(value, "refresh_token")
            expires = datetime.now(UTC) + timedelta(seconds=int(value.get("expires_in", 900)))
            userinfo = await client.get(
                "/oidc/userinfo", headers={"Authorization": f"Bearer {access}"}
            )
            profile = self._json_response(userinfo, "OIDC user info failed")
        subject = self._required(profile, "sub")
        try:
            account_id = f"account_{int(subject):016x}"
        except ValueError:
            account_id = subject
        state = RelayTokenState(access, refresh, expires, account_id)
        self.token_store.save(state)
        return state

    async def enroll_device(self, state: RelayTokenState, name: str) -> RelayTokenState:
        identity = self.credentials.load()
        payload = {
            "name": name,
            "public_key_jwk": identity.public_key_jwk,
            "key_thumbprint": identity.key_thumbprint,
        }
        value = await self._authorized_post(state, "/api/devices/enroll/", payload)
        device_id = self._required(value, "device_id")
        link_id = self._required(value, "link_id")
        mcp_url = self._required(value, "mcp_url")
        self.credentials.bind_remote_device(device_id)
        enrolled = RelayTokenState(
            state.access_token,
            state.refresh_token,
            state.expires_at,
            state.account_id,
            device_id,
            link_id,
            mcp_url,
        )
        self.token_store.save(enrolled)
        return enrolled

    async def resume_device(self, state: RelayTokenState, device_id: str) -> RelayTokenState:
        """Resume an existing local-key enrollment after interactive re-login.

        Re-login must never create a second relay device for an already-bound
        local key. The authenticated, account-scoped device list is authoritative
        for the current active link; proof of the retained key is required when
        the next WebSocket ticket is used.
        """

        identity = self.credentials.load()
        if identity.device_id != device_id or device_id.startswith("local_"):
            raise AgentError("enrollment_required", "Local device identity is not resumable")
        value = await self._authorized_get(state, "/api/devices/")
        devices = value.get("devices")
        if not isinstance(devices, list):
            raise AgentError("relay_protocol_error", "Relay device list is malformed")
        match = next(
            (
                item
                for item in devices
                if isinstance(item, dict) and item.get("device_id") == device_id
            ),
            None,
        )
        if match is None:
            raise AgentError(
                "enrollment_required",
                "This local device is not active for the signed-in account; "
                "explicit re-enrollment is required",
            )
        link_id = self._required(match, "link_id")
        resumed = RelayTokenState(
            access_token=state.access_token,
            refresh_token=state.refresh_token,
            expires_at=state.expires_at,
            account_id=state.account_id,
            device_id=device_id,
            link_id=link_id,
            mcp_url=f"{self.base_url}/mcp/d/{link_id}",
        )
        self.token_store.save(resumed)
        return resumed

    async def revoke_device(self, state: RelayTokenState, device_id: str) -> None:
        value = await self._authorized_post(state, f"/api/devices/{device_id}/revoke/", {})
        if value.get("revoked") is not True:
            raise AgentError("relay_protocol_error", "Relay did not confirm device revocation")

    async def issue_websocket_ticket(self) -> WebSocketTicket:
        state = await self.ensure_access_token()
        if state.device_id is None or state.link_id is None:
            raise AgentError("enrollment_required", "Device has not been enrolled with the relay")
        device_id = state.device_id
        value = await self._authorized_post(state, f"/api/devices/{device_id}/tickets/", {})
        # Link rotation is authoritative at the relay. Ticket issuance returns
        # the link snapshot bound to the next connection, allowing a running
        # agent to resynchronize without trusting unauthenticated local state.
        link_id = self._required(value, "link_id")
        mcp_url = self._required(value, "mcp_url")
        state = RelayTokenState(
            access_token=state.access_token,
            refresh_token=state.refresh_token,
            expires_at=state.expires_at,
            account_id=state.account_id,
            device_id=device_id,
            link_id=link_id,
            mcp_url=mcp_url,
        )
        self.token_store.save(state)
        expires = datetime.fromisoformat(self._required(value, "expires_at"))
        return WebSocketTicket(
            value=self._required(value, "ticket"),
            account_id=state.account_id,
            device_id=device_id,
            link_id=link_id,
            challenge=self._required(value, "challenge"),
            expires_at=expires.astimezone(UTC),
        )

    async def ensure_access_token(self) -> RelayTokenState:
        state = self.token_store.load()
        if state.expires_at > datetime.now(UTC) + timedelta(seconds=90):
            return state
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20) as client:
            response = await client.post(
                "/o/token/",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "refresh_token": state.refresh_token,
                    "resource": f"{self.base_url}/device-api",
                },
                headers={"Accept": "application/json"},
            )
            value = self._json_response(response, "OAuth refresh failed")
        refreshed = RelayTokenState(
            access_token=self._required(value, "access_token"),
            refresh_token=str(value.get("refresh_token") or state.refresh_token),
            expires_at=datetime.now(UTC) + timedelta(seconds=int(value.get("expires_in", 900))),
            account_id=state.account_id,
            device_id=state.device_id,
            link_id=state.link_id,
            mcp_url=state.mcp_url,
        )
        self.token_store.save(refreshed)
        return refreshed

    async def _authorized_post(
        self, state: RelayTokenState, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        state = await self.ensure_access_token() if state.expires_at <= datetime.now(UTC) else state
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20) as client:
            response = await client.post(
                path,
                json=payload,
                headers={
                    "Authorization": f"Bearer {state.access_token}",
                    "Accept": "application/json",
                },
            )
        return self._json_response(response, "Relay device request failed")

    async def _authorized_get(self, state: RelayTokenState, path: str) -> dict[str, Any]:
        state = await self.ensure_access_token() if state.expires_at <= datetime.now(UTC) else state
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20) as client:
            response = await client.get(
                path,
                headers={
                    "Authorization": f"Bearer {state.access_token}",
                    "Accept": "application/json",
                },
            )
        return self._json_response(response, "Relay device lookup failed")

    @staticmethod
    def _required(value: dict[str, Any], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item:
            raise AgentError("relay_protocol_error", f"Relay response omitted {key}")
        return item

    @staticmethod
    def _json_response(response: httpx.Response, message: str) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise AgentError("relay_protocol_error", message) from exc
        if response.is_error:
            code = (
                value.get("error", "relay_rejected")
                if isinstance(value, dict)
                else "relay_rejected"
            )
            raise AgentError(str(code), message, retryable=response.status_code >= 500)
        if not isinstance(value, dict):
            raise AgentError("relay_protocol_error", message)
        return value
