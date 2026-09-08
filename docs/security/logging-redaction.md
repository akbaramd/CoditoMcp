# Logging and audit redaction

Relay and agent emit structured JSON with UTC timestamp, severity, component,
event name, release version, safe actor IDs, operation/correlation ID, connection
epoch, state transition, duration/byte counts, and typed result code.

## Never record

- passwords, access/refresh tokens, cookies, authorization codes, WS tickets;
- invitation/reset URLs or raw selectors;
- private keys, proofs, approval signatures/nonces;
- request/response headers whose value can authenticate;
- local absolute paths or user profile names;
- file/patch contents, command/script/arguments, environment values, stdout/stderr;
- full OAuth client assertions, CIMD/JWKS fetch bodies, database connection strings.

Sanitized relative paths may appear only where useful and policy-approved; prefer
path/action digests and counts. Exception strings are passed through a centralized
redactor before serialization. Do not rely on application developers to redact each
log call independently.

## Audit events

Security events include login success/failure class, invite/reset issuance/use,
consent/grant/token-family changes, device enrollment/revocation/key fallback, link
rotation/revocation, project registration/mode change/root mismatch, operation
admission/state/terminal code, approval decision type, sandbox self-test, stale
socket/replay, and administrator action.

Audit rows are append-only to the application, tenant-filtered, paginated, and
retained 30 days. Database administrators remain able to alter storage; cryptographic
external transparency is post-MVP.

Tests feed canary secrets through every error path and fail if any canary reaches
captured logs, traces, metrics labels, UI flash messages, or audit payloads.
