from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_data_path

from .errors import AgentError

DEFAULT_RELAY_HTTP_URL = "https://codito.akbaramd.ir"
DEFAULT_RELAY_WEBSOCKET_URL = "wss://codito.akbaramd.ir/ws/device"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    relay_http_url: str = DEFAULT_RELAY_HTTP_URL
    relay_websocket_url: str = DEFAULT_RELAY_WEBSOCKET_URL
    desktop_client_id: str = "codito-windows-agent"
    data_directory: Path = field(default_factory=lambda: user_data_path("Codito", "Codito"))
    broker_path: Path | None = None
    log_level: str = "INFO"
    max_pending_operations: int = 32
    read_concurrency: int = 4

    @classmethod
    def load(cls, path: Path | None = None) -> AgentConfig:
        values: dict[str, object] = {}
        if path is not None:
            with path.open("rb") as stream:
                values = tomllib.load(stream)

        data_raw = str(values.get("data_directory", user_data_path("Codito", "Codito")))
        data_directory = Path(os.path.expandvars(data_raw)).expanduser()
        broker_raw = values.get("broker_path")
        broker_path = (
            Path(os.path.expandvars(str(broker_raw))).expanduser()
            if broker_raw is not None
            else None
        )
        config = cls(
            relay_http_url=str(values.get("relay_http_url", DEFAULT_RELAY_HTTP_URL)),
            relay_websocket_url=str(values.get("relay_websocket_url", DEFAULT_RELAY_WEBSOCKET_URL)),
            desktop_client_id=str(values.get("desktop_client_id", "codito-windows-agent")),
            data_directory=data_directory,
            broker_path=broker_path,
            log_level=str(values.get("log_level", "INFO")),
            max_pending_operations=int(str(values.get("max_pending_operations", 32))),
            read_concurrency=int(str(values.get("read_concurrency", 4))),
        )
        config.validate()
        return config

    def validate(self) -> None:
        http = urlparse(self.relay_http_url)
        websocket = urlparse(self.relay_websocket_url)
        if http.scheme != "https" or not http.netloc:
            raise AgentError("invalid_config", "relay_http_url must be an HTTPS URL")
        if websocket.scheme != "wss" or not websocket.netloc:
            raise AgentError("invalid_config", "relay_websocket_url must be a WSS URL")
        if not (1 <= self.max_pending_operations <= 128):
            raise AgentError("invalid_config", "max_pending_operations is out of range")
        if not (1 <= self.read_concurrency <= 16):
            raise AgentError("invalid_config", "read_concurrency is out of range")

    def ensure_directories(self) -> None:
        self.data_directory.mkdir(parents=True, exist_ok=True)
        (self.data_directory / "journals").mkdir(exist_ok=True)
