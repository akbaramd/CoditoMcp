# Executable MVP checklist

Last updated: 2026-09-08. A checked item needs reviewable evidence from the exact
release commit/artifact. “Code exists” is insufficient for a security control.

## Repository and documentation

- [x] `uv` Python 3.12 workspace, central Ruff/mypy/pytest configuration, shared
  protocol package, secret-safe examples, and contributor commands exist.
- [x] Research citations/access dates, ADRs, Mermaid architecture/sequences, threat
  model, approval policy, deployment/restore/rollback, and accepted limits are in Git.
- [ ] `uv lock --check`, formatting, lint, strict typing, all tests, and secret scan
  pass on a clean checkout.
- [ ] Branch protection requires CI and reviewed changes to trust boundaries.

Evidence:

```powershell
uv sync --frozen --all-packages --all-groups
uv run ruff format --check .
uv run ruff check .
$env:MYPYPATH = "packages/protocol/src;relay;agent/src"
uv run mypy -p codito_protocol -p codito_relay -p codito_agent
uv run pytest
git status --short
```

## Protocol and MCP surface

- [x] Shared Pydantic models define exactly `project_read`,
  `project_apply_patch`, and `project_shell`; unknown fields and basic unsafe paths
  are rejected; deterministic JSON Schemas can be exported.
- [ ] Runtime tool schemas/annotations/security schemes byte-for-byte match the
  shared contracts and MCP Inspector sees exactly three tools.
- [ ] Structured success + concise text and every typed error/retry rule are tested.
- [ ] Continuation tokens are signed, binding-preserving, expiring, and bounded.
- [ ] Cross-version and malformed/oversized envelope corpus fails closed.

Evidence:

```powershell
uv run --package codito-protocol pytest packages/protocol/tests --cov=codito_protocol --cov-fail-under=90
uv run --package codito-protocol python packages/protocol/scripts/export_schemas.py
git diff --exit-code -- packages/protocol/schemas
```

## Accounts and OAuth

- [ ] Interactive one-time admin bootstrap, 24h email-bound invite, invite signup,
  15m admin reset link, Argon2id, CSRF/session rules, and audit events pass tests.
- [ ] RFC 8414/OIDC/JWKS and path-specific RFC 9728 metadata produce only the public
  HTTPS URL.
- [ ] ChatGPT CIMD/private_key_jwt, exact redirect, PKCE S256, resource indicators,
  consent, and disabled open DCR pass positive/negative tests.
- [ ] Opaque 15m access tokens, rotating 30d refresh families/reuse revocation,
  revocation, introspection, account/grant/link/device/project/resource/scope binding
  all pass.
- [ ] CIMD/JWKS fetch defenses against SSRF, redirect, DNS rebinding, size/time
  exhaustion, and key confusion pass.

## Device routing and resilience

- [ ] CNG/TPM device enrollment and clearly labeled DPAPI fallback, short proof-bound
  WS tickets, key rotation/revocation, and project metadata sync pass.
- [ ] Ping 20s/timeout 20s, heartbeat 30s, offline 75s, full jitter 1–60s, credential
  refresh, and sleep/resume reconnect behave as documented.
- [ ] Epoch increment/old-socket fencing is atomic; two workers cannot cross-route.
- [ ] Relay persists before dispatch and agent SQLite persists before receipt ack.
- [ ] Queue 32, four reads/device, per-project patch/shell serialization, deadlines,
  cancellation, and terminal ack/backpressure pass.
- [ ] Offline mutation returns `device_offline`; patch dedupes; uncertain shell start
  returns `outcome_unknown`; output resumes ≤60s then Job dies.

## Windows filesystem and patch engine

- [ ] Project registration rejects every unsupported root and fingerprints volume +
  directory identity; replacement disables the project.
- [ ] Handle-walk tests cover traversal, drive/UNC/device/ADS/reserved aliases,
  Unicode/case, reparse swaps, hardlinks, placeholders, and TOCTOU.
- [ ] Reads/search are bounded and accurately report encoding/newlines/hash/size/
  numbering/truncation/continuation.
- [ ] Patch parser requires exact grammar/preconditions, rejects fuzzy/ambiguous
  hunks, and dry run has no writes.
- [ ] Multi-file staging/replacement/recovery converges atomically after injected
  crash at each journal/replace boundary.

## Shell sandbox and approvals

- [ ] .NET analyzers/xUnit pass; broker is self-contained and IPC authenticated.
- [ ] AppContainer self-test proves project-only access, no IPv4/IPv6/DNS/loopback,
  no inherited handles, child Job containment, kill/timeout/cancel, and fail-closed.
- [ ] Structured argv cannot become shell interpolation; PATH/PATHEXT/COMSPEC and
  environment are sanitized; script mode is explicit and displayed exactly.
- [ ] Toast only opens a persistent signed dialog with all required risk facts.
- [ ] Deny/once/eligible-session decisions bind exact digest and identities; replay,
  mismatch, expiry, spoofing, and lock/sleep/logout/restart/revoke/disconnect clear.
- [ ] Native approval is one-shot; native trusted opt-in shows the full authority
  warning; no model/relay input can choose mode or approval.
- [ ] No elevation, GUI, PTY/stdin, detach, or process breakaway is possible through
  the v1 API.

Evidence on Windows 11 x64:

```powershell
dotnet test agent/broker/Codito.Broker.sln --configuration Release
uv run --package codito-agent pytest agent/tests -m windows
```

## Observability and operations

- [ ] Structured logs, OpenTelemetry, authenticated Prometheus metrics, health/live
  and dependency/migration-ready checks, rate limits, and 30-day cleanup pass.
- [ ] Canary-secret tests find no tokens, selectors, proofs, absolute paths, content,
  commands/environment/output in logs/traces/metrics/audit/UI errors.
- [ ] Image builds as Linux amd64, scans clean, has SBOM/provenance, runs non-root
  read-only with dropped capabilities, and is pushed under version-SHA (not latest).
- [ ] Server `.env` is mode 0600; Compose migration-before-start, health, graceful
  shutdown, restart, immutable rollback, and disk alerts are verified.
- [ ] Daily backup retains 7 daily/4 weekly; checksum/age monitoring and documented
  isolated restore drill pass.
- [ ] Dependency audit, secret scan, Trivy, SBOM, and .NET dependency/analyzer gates
  pass or have reviewed time-bounded exceptions.

## Adversarial and acceptance gate

- [ ] All cases in `docs/security/threat-model.md` have automated or recorded manual
  tests against the packaged release.
- [ ] Relay worker, Redis, PostgreSQL, agent, and network restart at every transition
  does not lose/cross-route/replay mutation; shell uncertainty remains honest.
- [ ] Two users × two devices with duplicate project titles never cross account,
  grant, link, device, project, job, output, or idempotency boundary.
- [ ] Private ChatGPT connection lists projects, reads, patches, runs isolated shell,
  exercises native approval, revokes the link, and recovers after interruption.
- [ ] Public TLS/proxy is configured by the owner and OAuth/WSS/MCP work through
  `https://codito.akbaramd.ir`.
- [ ] Production Windows artifacts are signed, or release is explicitly limited to
  unsigned test use with warnings.
