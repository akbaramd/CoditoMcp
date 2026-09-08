from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

from .errors import AgentError


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    code: str
    verifier: str
    redirect_uri: str


CodeExchange = Callable[[AuthorizationResult], Awaitable[dict[str, object]]]


class DesktopOidcLogin:
    """System-browser authorization-code flow with PKCE and loopback callback."""

    def __init__(self, authorization_endpoint: str, client_id: str, resource: str) -> None:
        if urlparse(authorization_endpoint).scheme != "https":
            raise AgentError("invalid_config", "Authorization endpoint must use HTTPS")
        self.authorization_endpoint = authorization_endpoint
        self.client_id = client_id
        self.resource = resource

    async def authorize(self, *, timeout_seconds: float = 180) -> AuthorizationResult:
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        state = secrets.token_urlsafe(32)
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = (await reader.readline()).decode("ascii", errors="replace")
                target = line.split(" ", 2)[1]
                parsed = urlparse(target)
                from urllib.parse import parse_qs

                query = parse_qs(parsed.query)
                if query.get("state", [None])[0] != state:
                    status, body = "400 Bad Request", "Invalid login state."
                elif not query.get("code"):
                    status, body = "400 Bad Request", "Authorization code is missing."
                else:
                    status, body = "200 OK", "Codito sign-in completed. You may close this tab."
                    if not result.done():
                        result.set_result(query["code"][0])
                writer.write(
                    (
                        f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                        f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n{body}"
                    ).encode()
                )
                await writer.drain()
            except Exception as exc:
                if not result.done():
                    result.set_exception(exc)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(callback, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": "openid profile device:manage",
                "resource": self.resource,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        webbrowser.open(f"{self.authorization_endpoint}?{query}", new=2)
        try:
            code = await asyncio.wait_for(result, timeout_seconds)
            return AuthorizationResult(code, verifier, redirect_uri)
        except TimeoutError as exc:
            raise AgentError("login_timeout", "Desktop sign-in timed out") from exc
        finally:
            server.close()
            await server.wait_closed()
