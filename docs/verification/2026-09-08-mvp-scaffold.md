# MVP scaffold verification — 2026-09-08

This is development evidence for the initial working tree, not a production release
attestation.

## Passed

- Shared protocol: 52 pytest/Hypothesis tests passed.
- Direct Coverage.py run: 100% statement and branch coverage across
  `codito_protocol` at this checkpoint (521 statements, 96 branches).
- Shared protocol: Ruff check passed.
- Shared protocol: strict mypy package check passed.
- Shared protocol wheel built successfully and contains `codito_protocol/py.typed`.
- Eight deterministic JSON Schema snapshots exported successfully.
- `docker compose --env-file deploy/.env.example -f deploy/compose.yaml config
  --quiet` passed.
- GitHub workflow YAML parsed successfully.

## Pending or environment-blocked

- The relay Dockerfile was not built locally because the Docker Desktop Linux engine
  named pipe was not running. A release remains blocked until the image builds and
  runs/scans on Linux amd64 (locally, CI, or the `.39` server).
- Full relay/agent/.NET/integration/adversarial/end-to-end gates remain subject to
  root integration and the unchecked items in `docs/mvp-checklist.md`.
- Public DNS/TLS/proxy and real ChatGPT developer-mode acceptance are owner-managed
  and were not available at this checkpoint.

Sandbox-specific pytest/Ruff cache ACL warnings observed in the Codex worktree did
not affect the explicit no-cache protocol test/lint results and are not a product
runtime result.
