# Codito

Codito is a self-hosted, OAuth-protected bridge that lets ChatGPT work with
explicitly registered projects on a user's Windows device. The relay never learns
local absolute paths and the device maintains an outbound-only WebSocket tunnel.

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

The remote surface contains exactly three MCP tools: `project_read`,
`project_apply_patch`, and `project_shell`. See the [tool contract](docs/protocol/tools.md).

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
