# Codito relay

The relay is a Django 5.2 modular monolith mounted beneath a Starlette ASGI edge. It
serves the invite-only dashboard and Django OAuth Toolkit authorization server,
the MCP Python SDK v2 endpoint, health/metrics routes, and the authenticated device
WebSocket gateway from one deployable image.

## Local development

From the repository root:

```powershell
uv sync --all-packages
$env:DJANGO_DEBUG = "true"
uv run python relay/manage.py migrate
uv run python relay/manage.py bootstrap_admin
uv run uvicorn codito_relay.asgi:application --app-dir relay --reload --no-access-log
```

Redis is required for live device routing. SQLite is selected only when
`DATABASE_URL` is absent; production startup must provide PostgreSQL and Redis.

Useful administrative commands:

```powershell
uv run python relay/manage.py issue_invitation user@example.com --created-by admin
uv run python relay/manage.py issue_password_reset user@example.com --created-by admin
uv run python relay/manage.py provision_desktop_client
uv run python relay/manage.py cleanup_codito --dry-run
```

Invitation/reset URLs are bearer links and should be delivered through a private
channel. They are printed once, never persisted in plaintext, and expire after 24
hours and 15 minutes respectively. Production Uvicorn access logging must remain
disabled (`--no-access-log`) because those selectors necessarily appear in request
paths; structured application audit events never include them.

## Security invariants

- `/mcp/d/{link_id}` treats `link_id` as a route, never a credential.
- MCP access requires an opaque bearer token whose user, durable grant binding,
  exact RFC 8707 resource, link, device, project, and runtime scopes all match.
- OAuth tokens are stored only by SHA-256 lookup checksum via DOT's RFC 9700 mode.
- Open dynamic registration is not mounted. ChatGPT CIMD is enabled only for the
  configured host allowlist; the desktop client is explicitly pre-provisioned.
- CIMD clients currently use authorization code + PKCE with
  `token_endpoint_auth_method=none`. When an approved ChatGPT metadata document
  prefers `private_key_jwt` but explicitly lists `none` in
  `token_endpoint_auth_methods_supported`, Codito persists the declared public
  fallback. It never downgrades metadata that does not explicitly support `none`.
  Django OAuth Toolkit 3.4.1 does not verify `private_key_jwt`; Codito therefore
  advertises only `none` until assertion verification with persisted JWKS and JTI
  replay protection is implemented.
- Device sockets use a single-use 60-second ticket plus a signature from the
  enrolled device key. Each reconnect increments a fenced connection epoch.
- A device cannot move a caller-supplied project ID across accounts or devices.
- The relay stores project titles and root identity fingerprints, never local paths.

The ASGI server must be started with WebSocket protocol pings configured to 20
seconds (for Uvicorn, `--ws-ping-interval 20 --ws-ping-timeout 20`). The device
protocol also has 30-second application heartbeats and a 75-second silence cutoff.
