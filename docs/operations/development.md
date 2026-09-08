# Local development

## Python

`uv` provisions Python 3.12 from `.python-version`; do not require or mutate a system
Python installation.

```powershell
uv sync --all-packages --all-groups
uv run ruff format --check .
uv run ruff check .
$env:MYPYPATH = "packages/protocol/src;relay;agent/src"
uv run mypy -p codito_protocol -p codito_relay -p codito_agent
uv run pytest
```

Use a synthetic local `.env`; it is ignored. Never copy production secrets or a
database dump into a development checkout.

## Relay dependencies

Integration tests should start disposable PostgreSQL and Redis containers on
ephemeral ports, apply migrations, and destroy only those explicitly named test
resources. SQLite is useful for unit tests but does not validate PostgreSQL locks,
constraints, isolation, or Redis consumer behavior.

Run the ASGI application from the workspace:

```powershell
uv run --package codito-relay uvicorn codito_relay.asgi:application `
  --app-dir relay --host 127.0.0.1 --port 8094
```

## Windows agent

Daemon/UI/broker tests that assert Win32 security run on Windows 11 x64. Tests must
use a dedicated temporary directory and AppContainer profile namespace and clean it
up without traversing unrelated filesystem locations.

```powershell
dotnet test agent/broker/Codito.Broker.sln --configuration Release
uv run --package codito-agent pytest agent/tests -m windows
```

An unsigned development package will produce Windows trust warnings. Never weaken
SmartScreen, execution policy, or enterprise controls in automated setup.
