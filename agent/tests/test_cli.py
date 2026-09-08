from __future__ import annotations

from pathlib import Path

import pytest

from codito_agent.__main__ import _require_trusted_ack, _startup_arguments
from codito_agent.errors import AgentError
from codito_agent.models import ProjectMode


def test_startup_registers_daemon_and_tray_with_global_config_first() -> None:
    config = Path(r"C:\Codito Config\agent.toml")
    daemon, tray = _startup_arguments(config)
    assert daemon == ["-m", "codito_agent", "--config", str(config), "daemon"]
    assert tray == ["-m", "codito_agent.ui", "--config", str(config), "--minimized"]


def test_native_trusted_registration_requires_explicit_authority_ack() -> None:
    with pytest.raises(AgentError) as error:
        _require_trusted_ack(ProjectMode.NATIVE_TRUSTED, False)
    assert error.value.code == "confirmation_required"
    _require_trusted_ack(ProjectMode.NATIVE_TRUSTED, True)
    _require_trusted_ack(ProjectMode.ISOLATED, False)
