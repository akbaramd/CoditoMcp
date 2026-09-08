# Approved device access and installed Windows tools

Implementation: `device_read` and explicit `project_shell.execution=native_approval`.
Decision: [ADR 0012](../adr/0012-approved-device-reads.md).

## Verification on the development Windows machine

- Python suites (agent, shared protocol, relay): 240 passed.
- Ruff lint and formatting: passed. Strict mypy: 71 source files passed.
- Django migration drift check: no changes detected; migration 0008 included.
- .NET broker tests: 3 passed.
- Real installed Windows broker, read-only CLI smoke: `uv --version` exited 0
  (0.12.5); `dotnet --version` exited 0 (10.0.400), using the new native environment.
- The CLI smoke is not a claim of completed interactive ChatGPT/Windows consent
  acceptance. That requires the user to make the actual Windows decision.

## Acceptance after relay + desktop update

1. Refresh the connector's tool definitions in ChatGPT. Verify `device_read` appears.
2. Ask to inspect a specific directory. Verify Windows shows its scope, exact
   operation, purpose, requesting identity, and private-data warning.
3. Deny: no directory entries/file content should return.
4. Retry, Allow once: only that request proceeds; another request prompts again.
5. Always allow read: further reads under the same `scope_path` by the same
   connection identity proceed. Changing scope/account/link/grant prompts again.
6. Settings -> Review / revoke saved read permissions -> Revoke all. Next read
   prompts again. No relay/MCP API can approve or delete this local policy.
7. Request native tool execution; the shell prompt must appear even for an isolated
   project. No read grant suppresses it. Allow once then run `uv --version` and
   `dotnet --version`. A native PowerShell script can use Set-Location for another
   directory after approval; its full filesystem/network authority is explicit.
8. Offline/revoked devices must fail without running work. No approval response
   from a previous connection may authorize a newly connected operation.

Known limits: no elevation, PTY, GUI automation, or interactive shell stdin. Reads
do not bypass Windows ACLs or follow reparse/placeholder/hardlink targets. A native
process has the logged-in user's authority, not an enforceable project boundary.
Saved directory read permissions require specifying that same `scope_path` in
later calls and selecting descendants via the relative `path` parameter.

## Release and deployment evidence

- Published GitHub release: v0.1.7, source
  `9f8a97728cb1ed3a0f631e76ca07b68fe849d2d3` (feature commit `c28c43e`).
- CI 34221333552 and Windows release 34221333528 both completed successfully,
  including dependency/secret scans and PostgreSQL/Redis integration jobs.
- Windows ZIP: 140,023,704 bytes; SHA-256
  `7c50013af6029abdfd2f42897905c6536c9fec9b8e272d5ea691798ae68b7a8c`.
  Installed through the checksum-verifying bootstrap. Both running executables
  are from the per-user `app/0.1.7` directory; current-version is 0.1.7.
- Relay image: `docker.wa-nezam.org/codito/relay:0.1.7-9f8a97728cb1`;
  digest `sha256:c55b586c989bee72b33020f69efd80bb7dd7c47ea391c23d612678b118aa54a4`.
- Pre-deployment backup:
  `/data/backups/codito/daily/codito-20260908T114317Z.dump`.
  Migration 0008 applied successfully. Only relay was recreated; existing shared
  PostgreSQL/Redis containers and networks were reused without pulling images.
- Public `/health/ready`: 0.1.7, database/Redis/OIDC signing key all `ok`.
  Unauthenticated MCP still returns 401 with the device-specific OAuth challenge.
- Windows GUI visibly reports 0.1.7 and ONLINE; local IPC and relay DB agree on
  connection epoch 128. Relay heartbeat age observed 3.7 seconds. Five tool
  contracts are loaded in the deployed relay, including `device_read`.
- Important existing local configuration: CoditoMcp was already `native_trusted`.
  This setting was preserved, not silently changed. Default project-policy commands
  inherit that existing trust; `execution=native_approval` always requests consent.
  To require consent for every native command, the owner must choose Native approval
  in the project's local Windows execution-policy selector.
- Final interactive acceptance still requires the user to refresh ChatGPT's tool
  list and make a real Windows consent decision. No security dialog was approved
  by automation.
