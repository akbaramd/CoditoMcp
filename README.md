# Codito

## Native Windows work and screenshots

Choose **Project access** locally to use installed tools (`uv`, `.NET`, Git,
SSH, Docker) for prompt-free project work. Outside cwd / declared external paths
require Windows Deny/Allow/Always allow. This is cooperative native approval routing,
not an OS sandbox; arbitrary native commands still have the Windows user's authority.
Shell starts return a stable job ID and use a bounded event-driven wait for approval/state
changes. If the start result is still non-terminal, long-poll/cancel that same job; never
resubmit the intended command merely to continue an approval flow.

`screen_list` lists monitors; `screenshot_capture` sends a selected-display PNG to ChatGPT.
Re-authorize `screen:read` when adding it to an existing connection. Windows offers
Deny / Allow / Always allow for eligible display configurations; saved screen access
is separate from file/shell permissions and is revocable in Settings.

Example tool sequence (replace `screen_<id>` with an ID returned by the first call):

```text
screen_list(purpose="Choose the monitor to inspect")
browser_open(project_id="<project-id>", browser="firefox", url="https://example.com/",
             purpose="Open the requested page in Firefox")
screenshot_capture(project_id="<project-id>", display="screen_<id>", purpose="Inspect monitor")
```

Browser opening requires `shell:execute` and separate one-shot Windows approval,
unless **Full device access** was explicitly selected for the calling project.
It submits the URL to Firefox or the default browser; it cannot confirm page load.
There is no mouse/keyboard control, elevation or secure-desktop capture. Notifications
do not automatically bring Codito to the foreground. See the
[tool contracts](docs/protocol/tools.md),
[consent decision](docs/adr/0015-selected-display-and-browser-consent.md), and
[acceptance guide](docs/operations/2026-09-08-approval-and-screen-release.md).

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

The public catalog contains 31 focused tools: project list/add/rename/remove,
directory listing, `file_read`, `text_search`, `file_patch`, `file_delete`,
`execute_shell`, `shell_status`, `shell_cancel`, `screen_list`,
`screenshot_capture`, `browser_open`, plus 16 language-neutral `code_*` tools for
workspace summary, symbols, definitions/references, diagnostics/hover, context,
hierarchies, impact/architecture/dependencies/related tests and explicit reindex. Old
names remain hidden compatibility aliases. See the
[tool contract](docs/protocol/tools.md) and
[Code Intelligence guide](docs/operations/code-intelligence.md).

`execute_shell` takes the full `command` string, `executor` (`powershell` or `cmd`),
`timeout_seconds`, project ID, cwd, purpose and idempotency key. It does not need an
action enum. File tools accept an optional requested `scope_path` for approved
external work; patch/delete retain exact hashes and recovery journaling.

Project settings offer **Ask every time**, **Project access**, and explicit
**Full device access**. Existing legacy trust is not silently upgraded. Saved
permissions can be inspected/refreshed and revoked individually in Settings.
Full access still requires valid OAuth scopes and never permits locked-desktop
capture or elevation. See [local policy](docs/security/approval-policy.md).

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
- `full_access` explicitly permits native file/shell/desktop authority without
  local prompts. Legacy `native_trusted` is not silently upgraded to this new mode.

Report vulnerabilities according to [`SECURITY.md`](SECURITY.md).
# Approved access to Windows outside projects

Use `directory_list` to request a drive/directory read without registering it as a
project. Supply the origin `project_id`, `scope_path=C:/`, `path=""`,
`purpose="Inspect the requested drive"`. Windows asks **Deny**, **Allow once**, or
**Always allow reading this folder**. Always allow is local, revocable in Settings,
and read-only. Broad scopes can expose private files to the requesting client.

For installed Windows tools such as `uv`, `dotnet`, or `git`, use
`execute_shell` with a full `command`, for example `dotnet build`. The local project
mode controls approval. Native commands use the installed tool PATH and the
logged-in user's filesystem/network authority. Project cwd is not an isolation
boundary. Legacy isolated mode remains blocked until changed locally.

Both relay and desktop must be updated for these capabilities. Refresh the
ChatGPT connector tool list after updating.
