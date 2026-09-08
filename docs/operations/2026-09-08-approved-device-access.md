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
