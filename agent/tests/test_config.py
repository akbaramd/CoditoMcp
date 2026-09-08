import sys
from pathlib import Path

import pytest

from codito_agent.config import AgentConfig


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
    assert AgentConfig.load().broker_path is None


def test_explicit_broker_is_not_overridden(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("codito_agent.config.tomllib.load", lambda _: {"broker_path": "custom.exe"})
    assert AgentConfig.load(Path(__file__)).broker_path == Path("custom.exe")
