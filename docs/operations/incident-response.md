# Incident response

## Immediate priorities

1. Protect people/data and stop additional unsafe execution.
2. Preserve enough redacted evidence to understand scope.
3. Revoke the narrowest affected credentials, escalating to all grants/devices/keys
   when boundaries are uncertain.
4. Restore only from known-good immutable artifacts and verified data.

## Scenarios

| Signal | Immediate action |
|---|---|
| Token/grant/link exposure | Revoke grant and link, terminate affected operations, review tenant audit, reauthorize |
| Device key compromise | Revoke device and all links/tickets, close epochs, remove startup agent, re-enroll after host cleanup |
| OAuth signing key compromise | Remove key after emergency rollover, revoke affected tokens/families, publish new JWKS, reauthenticate |
| Cross-tenant routing | Disable MCP admission globally, preserve DB/audit state, revoke suspect grants, determine every affected operation |
| Sandbox escape/self-test regression | Disable isolated shell feature/release, terminate Job Objects, revoke remote shell grants if needed, patch/retest |
| Stale socket duplicate | Fence/close both connections, stop mutations, reconcile journals/hashes, mark shell uncertainty honestly |
| Database loss/corruption | Stop relay, take forensic copy, restore verified backup, rebuild Redis routing, force epoch reconnect |

Do not include tokens, full commands, file content, local roots, or sensitive output
in incident chat/tickets. Use operation/action digests and transfer sensitive evidence
through an approved encrypted channel.

## Recovery gate

Before reopening admission: root cause bounded, compromised credentials revoked,
patched image identified by digest, migrations/data verified, relevant adversarial
test added and passing, sandbox/epoch/cross-tenant smoke tests passing, and owner
records the residual risk.

## OAuth / ChatGPT connection diagnostics

The relay emits JSON events to container stdout for login, OAuth discovery,
authorization, token exchange, UserInfo and device MCP HTTP requests. No debug
mode or packet capture is needed. Compose caps the JSON log files at five 10 MB
files per container. They are
short-lived operational diagnostics, not a replacement for durable audit events.

From `/data/projects/codito`:

```sh
docker compose --env-file .env -f compose.yaml logs --since 10m --tail 500 relay
```

`http.start`, `http.response`, and `http.end` share a server-generated `request_id`
with `oauth.outcome`, validator events and MCP authorization failures within the
same request. The response also includes `X-Codito-Request-ID`. IDs do not span
separate OAuth requests; match chronological phases and the user's retry time.
ASGI context isolation preserves IDs across concurrent requests and Django's
thread handoff. Streaming bodies are passed through untouched.

- No `http.start`: the request has not reached this application; inspect the
  client's flow and the external DNS/TLS/proxy layer.
- `oauth.scopes_rejected` plus `oauth.outcome.error=invalid_scope`: inspect the
  allowlisted requested scopes and client policy. A 302 can be a failed OAuth
  callback; HTTP status alone does not indicate success.
- `oauth.outcome.authorization_code_issued=true`: authorization reached the
  callback. If no token request follows, investigate client callback handling.
- `oauth.assertion_rejected`: `stage` distinguishes assertion structure, client
  registration, metadata/JWKS fetch, key selection, signature, claims/time/audience
  validation and replay protection. Exception text and JWTs are never logged.
- `oauth.outcome.access_token_issued=true`: token exchange succeeded; follow the
  next `mcp` events to distinguish authorization rejection from transport failure.
- `http.exception` or `oauth.diagnostic_unavailable`: a fixed marker is recorded,
  never exception text that could embed credentials.

Only known scope names and OAuth error codes are emitted. Unknown scope values
are counted, not copied; request/response bodies, full URLs, headers, passwords,
cookies, token values, assertions, authorization codes, account identifiers, and
local file paths are not captured. Keep raw Uvicorn access logging and oauthlib
DEBUG logging disabled. Diagnostic collection does not grant permissions or fix
an underlying OAuth policy mismatch.
