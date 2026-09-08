# Codito Windows agent

The agent is a per-user Windows 11 daemon with a PySide6 desktop dashboard and
notification-area companion. The dashboard shows the current device, revocable
MCP endpoint, relay state, project policies, recent operation journal, and local
approval dialogs without sending absolute project paths to the relay.
It keeps absolute project paths and device credentials local, journals every remote
operation before acknowledging it, and maintains one outbound authenticated WSS
connection to the Codito relay.

## Security modes

- **Isolated** is the default. A command runs only when the .NET broker proves all
  AppContainer, project ACL, network-denial, and child-containment self-tests. The
  current MVP broker deliberately fails this proof until those AppContainer tests
  are implemented; it never falls back to native execution.
- **Native approval** uses the logged-in user's full filesystem and network
  authority after a one-shot local dialog. A working directory inside a project is
  not a security boundary.
- **Native trusted** is a strong, explicit local opt-in. Commands still use bounded
  output, timeouts, sanitized environment, and a kill-on-close Job Object, but they
  otherwise have the logged-in user's authority and do not prompt.

Project registration permits only fixed NTFS/ReFS directories and rejects drive
roots, UNC/removable/cloud-placeholder roots, and any reparse traversal. Each root
is bound to its volume/file identity. File mutations reject hard links, revalidate
parent/target identities and content hashes, use sibling temporary files, and use
a crash-recovery journal. Python's mutation path is not yet a true handle-relative
Win32 implementation; enabling isolated writes for production remains blocked on
moving that final operation into the native broker.

## Local development

From the repository root (Python is provisioned by `uv`):

```powershell
uv sync --all-packages --all-extras
uv run --package codito-agent pytest agent/tests
uv run ruff check agent
uv run mypy agent/src/codito_agent
dotnet test agent/broker/Codito.Broker.sln --configuration Release --maxcpucount:1
```

Copy `config.example.toml` into `%LOCALAPPDATA%\Codito\config.toml`. Do not put
tokens in that file. Device tokens and the software-key fallback are protected
with current-user DPAPI; a CNG/TPM key will be preferred when the broker verb is
available.

Useful commands:

```powershell
uv run codito-agent init
uv run codito-agent login
uv run codito-agent register-project --title "My project" --path C:\src\project
uv run codito-agent register-project --title "Trusted" --path C:\src\trusted --mode native_trusted --acknowledge-full-user-authority
uv run codito-agent list-projects
uv run codito-agent startup enable
uv run codito-agent daemon
uv run codito-agent-ui
```

`startup enable` registers both the daemon and approval tray for the current user;
approval-capable operation requires the tray (or another authenticated IPC UI) to
be running. The agent is not a Windows service and never requests elevation.
Locked PyInstaller entrypoints, the
self-contained broker publish, deterministic developer ZIP, per-user WiX authoring,
and explicit signing hook live in [`packaging/`](packaging/). See
[`../docs/operations/windows-release.md`](../docs/operations/windows-release.md).

## Broker contract

The Python process invokes the self-contained `.NET 8` broker over a bounded JSON
stdin/stdout contract. The broker owns Job Object and future AppContainer/CNG and
handle-safe operations. See [`broker/README.md`](broker/README.md).
