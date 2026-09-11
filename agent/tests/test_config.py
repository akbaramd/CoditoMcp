import sys
from pathlib import Path

import pytest

from codito_agent.config import AgentConfig
from codito_agent.errors import AgentError


@pytest.mark.parametrize("component", ["tray", "daemon"])
def test_frozen_components_resolve_sibling_broker(monkeypatch, tmp_path, component):
    executable = tmp_path / "app" / "0.1.8" / component / f"codito-agent-{component}.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.chdir(tmp_path)
    assert (
        AgentConfig.load().broker_path == executable.parent.parent / "broker" / "Codito.Broker.exe"
    )


def test_source_execution_does_not_guess_broker(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    config = AgentConfig.load()
    assert config.broker_path is None
    assert config.read_concurrency == 8
    assert config.read_concurrency_per_project == 4


def test_explicit_broker_is_not_overridden(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("codito_agent.config.tomllib.load", lambda _: {"broker_path": "custom.exe"})
    assert AgentConfig.load(Path(__file__)).broker_path == Path("custom.exe")


def test_per_project_read_limit_cannot_exceed_global_limit():
    with pytest.raises(AgentError, match="read_concurrency_per_project"):
        AgentConfig(read_concurrency=2, read_concurrency_per_project=3).validate()


def test_legacy_lower_read_concurrency_gets_compatible_project_limit(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("read_concurrency = 2\n", encoding="utf-8")

    config = AgentConfig.load(path)

    assert config.read_concurrency == 2
    assert config.read_concurrency_per_project == 2
