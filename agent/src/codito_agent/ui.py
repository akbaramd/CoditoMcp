from __future__ import annotations

import argparse
from pathlib import Path

from .config import AgentConfig
from .ipc import IpcSecretStore, NamedPipeClient, default_pipe_name


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="codito-agent-ui")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Start in the notification area instead of opening the dashboard",
    )
    parser.add_argument("--show-status", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--toast-activation", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        from .desktop_ui import run_desktop
    except ImportError as exc:
        raise SystemExit(
            "The Codito desktop UI could not start. Install the 'ui' extra or repair the "
            f"application package. Technical detail: {exc}"
        ) from exc

    config = AgentConfig.load(arguments.config)
    from .diagnostics import configure

    configure(config.data_directory, "desktop")
    key = IpcSecretStore(config.data_directory / "ipc-key.dpapi").load_or_create()
    client = NamedPipeClient(default_pipe_name(), key)
    if arguments.toast_activation:
        from .diagnostics import event
        from .notifications import activation_request

        try:
            result = client.request(activation_request(arguments.toast_activation))
            event("toast_activation", detail="accepted" if result.get("ok") else "rejected")
        except Exception as exc:
            event("toast_activation_failed", detail=type(exc).__name__)
        return  # Never start a second desktop instance on notification activation.
    minimized = arguments.minimized and not arguments.show_status
    raise SystemExit(run_desktop(config, client, minimized=minimized))


if __name__ == "__main__":
    main()
