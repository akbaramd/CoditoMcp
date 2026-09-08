# Codito

Codito is a self-hosted, OAuth-protected bridge that lets ChatGPT work with
registered projects on a user's Windows device, with Windows-approved access
outside projects when requested. Registered roots stay local; explicitly requested
device-read scopes/results pass through the relay. The device uses an outbound
WebSocket tunnel.

> **MVP status:** active development. Do not expose this build to untrusted users
> until every release-blocking item in [`docs/mvp-checklist.md`](docs/mvp-checklist.md)
> is checked and the Windows sandbox test suite passes on the target build.

## Repository layout

```text
agent/              Windows per-user daemon, tray UI, and .NET security broker
relay/              Django/OAuth/MCP/ASGI relay
packages/protocol/  Shared Pydantic wire contracts and JSON Schemas
deploy/             Immutable container and Compose deployment assets
docs/               Decisions, research, threat model, and runbooks
```

The remote surface contains five bounded MCP tools: `project_read`,
`project_apply_patch`, `project_shell`, locally governed `project_manage`, and
Windows-approved `device_read`. See
the [tool contract](docs/protocol/tools.md).

## Install on Windows 11 x64

After a stable GitHub release is published, inspect and run the per-user
bootstrap from PowerShell:

```powershell
irm https://raw.githubusercontent.com/akbaramd/CoditoMcp/main/scripts/bootstrap-windows.ps1 | iex
```

The bootstrap resolves the latest stable release through GitHub's API and refuses
an archive without the expected GitHub-published SHA-256 digest. It installs under
`%LOCALAPPDATA%\Codito\app\<version>`, registers the daemon and desktop dashboard
for the current user's sign-in, and keeps credentials/project paths in the separate
local data directory. No administrator rights or system Python installation is
required. Until production code signing is configured, Windows can display an
unknown-publisher warning even though both outer and inner SHA-256 manifests are
verified.

## Developer setup

Install [uv](https://docs.astral.sh/uv/) and .NET 8 on Windows. A system Python is
not required.

```powershell
uv sync --all-packages --all-groups
uv run ruff format --check .
uv run ruff check .
$env:MYPYPATH = "packages/protocol/src;relay;agent/src"
uv run mypy -p codito_protocol -p codito_relay -p codito_agent
uv run pytest
dotnet test agent/broker/Codito.Broker.sln --configuration Release
```

Useful commands are also available through `just` when installed. Copy
`.env.example` only for local development; production configuration is created on
the server and must never enter Git.

## Security boundary

- OAuth authorization and the opaque device link are both required.
- Project roots are selected locally and absolute paths stay on the device.
- Isolated shell execution must fail closed if the Windows broker cannot prove its
  AppContainer and Job Object controls.
- `native_trusted` intentionally gives commands the full authority of the logged-in
  Windows user and must be enabled locally with a prominent warning.

Report vulnerabilities according to [`SECURITY.md`](SECURITY.md).
# Approved access to Windows outside projects

Use `device_read` to request a drive/directory read without registering it as a
project. For example: `operation=list_directory`, `scope_path=C:/`, `path=""`,
`purpose="Inspect the requested drive"`. Windows asks **Deny**, **Allow once**, or
**Always allow reading this folder**. Always allow is local, revocable in Settings,
and read-only. Broad scopes can expose private files to the requesting client.

For installed Windows tools such as `uv`, `dotnet`, or `git`, use
`project_shell` with `execution=native_approval`. This requests one-shot Windows
approval, uses the installed tool PATH, and grants the resulting command the
logged-in user's full filesystem/network authority. Native scripts can change
directories after approval. Project cwd is not an isolation boundary. Existing
isolated mode remains fail-closed; it does not silently fall back to native.

Both relay and desktop must be updated for these capabilities. Refresh the
ChatGPT connector tool list after updating.
