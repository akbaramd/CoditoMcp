"""Windowless packaged entry point for the per-user Codito daemon."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
from pathlib import Path

from codito_agent.config import AgentConfig
from codito_agent.daemon import CoditoDaemon
from codito_agent.errors import AgentError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codito-agent-daemon")
    parser.add_argument("--config", type=Path, help="Path to agent TOML configuration")
    return parser


def _packaged_broker() -> Path:
    # The release layout places daemon/ and broker/ next to one another. Using
    # sys.executable (instead of PyInstaller's extraction directory) works for
    # both onedir builds and the installed MSI payload.
    return Path(sys.executable).resolve().parent.parent / "broker" / "Codito.Broker.exe"


def main() -> None:
    arguments = _parser().parse_args()
    try:
        config = AgentConfig.load(arguments.config)
        if config.broker_path is None:
            config = dataclasses.replace(config, broker_path=_packaged_broker())
        asyncio.run(CoditoDaemon(config).run())
    except AgentError as exc:
        # There is intentionally no console. A short, stable exit code allows
        # the tray and Windows event/process monitors to report startup failure.
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
