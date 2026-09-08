set shell := ["pwsh", "-NoLogo", "-NoProfile", "-Command"]

sync:
    uv sync --all-packages --all-groups

format:
    uv run ruff format .
    uv run ruff check --fix .

lint:
    uv run ruff format --check .
    uv run ruff check .
    $env:MYPYPATH = "packages/protocol/src;relay;agent/src"; uv run mypy -p codito_protocol -p codito_relay -p codito_agent

test:
    uv run pytest

schemas:
    uv run --package codito-protocol python packages/protocol/scripts/export_schemas.py

broker-test:
    dotnet test agent/broker/Codito.Broker.sln --configuration Release

quality: lint test broker-test
