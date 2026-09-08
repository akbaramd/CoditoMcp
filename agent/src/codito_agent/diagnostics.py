"""Bounded local diagnostics: never record commands, paths, credentials or bodies."""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

_logger = logging.getLogger("codito.local")


def event(name: str, identifier: str = "", detail: str = "") -> None:
    _logger.info(
        json.dumps(
            {"at": datetime.now(UTC).isoformat(), "event": name, "id": identifier, "detail": detail}
        )
    )


def configure(directory: Path, component: str) -> None:
    directory = directory / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / f"{component}.jsonl", maxBytes=1_048_576, backupCount=3, encoding="utf-8"
    )
    for old in _logger.handlers:
        old.close()
    _logger.handlers[:] = [handler]
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

    def exception_hook(
        kind: type[BaseException], value: BaseException, trace: TracebackType | None
    ) -> None:
        del value  # exception messages can contain secrets or user command bodies
        frames = traceback.extract_tb(trace)
        location = ";".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames)
        event("uncaught_exception", detail=f"{kind.__name__}:{location}")

    sys.excepthook = exception_hook
    event("component_started", detail=component)
