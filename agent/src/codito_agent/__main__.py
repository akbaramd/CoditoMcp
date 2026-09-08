from __future__ import annotations

import argparse
import asyncio
import platform
import sys
from pathlib import Path

from .config import AgentConfig
from .credentials import DeviceCredentialStore
from .daemon import CoditoDaemon
from .db import AgentDatabase
from .errors import AgentError
from .models import ProjectMode
from .oidc import DesktopOidcLogin
from .relay_client import RelayClient, RelayTokenStore
from .startup import disable_startup, enable_startup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codito-agent")
    parser.add_argument("--config", type=Path, help="Path to agent TOML configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create local data and a device signing key")
    commands.add_parser("login", help="Sign in with the system browser and enroll this device")
    commands.add_parser("daemon", help="Run the background agent")

    register = commands.add_parser("register-project", help="Register a fixed local directory")
    register.add_argument("--title", required=True)
    register.add_argument("--path", type=Path, required=True)
    register.add_argument(
        "--mode", choices=[mode.value for mode in ProjectMode], default="isolated"
    )
    register.add_argument(
        "--acknowledge-full-user-authority",
        action="store_true",
        help="Required when registering directly in native_trusted mode",
    )

    commands.add_parser("list-projects", help="List locally registered projects")
    mode = commands.add_parser("set-mode", help="Set a project's local execution policy")
    mode.add_argument("project_id")
    mode.add_argument("mode", choices=[item.value for item in ProjectMode])
    mode.add_argument(
        "--acknowledge-full-user-authority",
        action="store_true",
        help="Required when selecting native_trusted",
    )
    startup = commands.add_parser("startup", help="Manage per-user sign-in startup")
    startup.add_argument("state", choices=["enable", "disable"])
    return parser


async def _login(config: AgentConfig) -> None:
    config.ensure_directories()
    credentials = DeviceCredentialStore(config.data_directory / "credentials")
    if credentials.metadata_path.exists():
        identity = credentials.load()
    else:
        identity = credentials.enroll()
    token_store = RelayTokenStore(config.data_directory / "tokens.dpapi")
    relay = RelayClient(config.relay_http_url, config.desktop_client_id, token_store, credentials)
    login = DesktopOidcLogin(
        f"{config.relay_http_url.rstrip('/')}/o/authorize/",
        config.desktop_client_id,
        f"{config.relay_http_url.rstrip('/')}/device-api",
    )
    authorization = await login.authorize()
    state = await relay.exchange_code(authorization)
    if identity.device_id.startswith("local_"):
        enrolled = await relay.enroll_device(state, platform.node() or "Windows device")
    else:
        enrolled = await relay.resume_device(state, identity.device_id)
    print(f"Enrolled device {enrolled.device_id}.")
    print(f"ChatGPT MCP URL: {enrolled.mcp_url}")
    if identity.hardware_backed:
        print("Device key: TPM/CNG hardware-backed")
    else:
        print("Device key: DPAPI-protected software fallback")


def _database(config: AgentConfig) -> AgentDatabase:
    config.ensure_directories()
    return AgentDatabase(config.data_directory / "agent.sqlite3")


def _require_trusted_ack(mode: ProjectMode, acknowledged: bool) -> None:
    if mode is ProjectMode.NATIVE_TRUSTED and not acknowledged:
        raise AgentError(
            "confirmation_required",
            "native_trusted grants commands your full filesystem and network authority; "
            "repeat with --acknowledge-full-user-authority",
        )


def _startup_arguments(config_path: Path | None) -> tuple[list[str], list[str]]:
    common = ["-m", "codito_agent"]
    ui = ["-m", "codito_agent.ui"]
    if config_path is not None:
        common.extend(["--config", str(config_path)])
        ui.extend(["--config", str(config_path)])
    return [*common, "daemon"], ui


def main() -> None:
    arguments = _parser().parse_args()
    try:
        config = AgentConfig.load(arguments.config)
        if arguments.command == "init":
            config.ensure_directories()
            credentials = DeviceCredentialStore(config.data_directory / "credentials")
            identity = (
                credentials.load() if credentials.metadata_path.exists() else credentials.enroll()
            )
            print(f"Local device key ready: {identity.key_id}")
        elif arguments.command == "login":
            asyncio.run(_login(config))
        elif arguments.command == "register-project":
            selected_mode = ProjectMode(arguments.mode)
            _require_trusted_ack(selected_mode, arguments.acknowledge_full_user_authority)
            project = _database(config).register_project(
                arguments.title,
                arguments.path,
                selected_mode,
            )
            print(f"Registered {project.title}: {project.project_id}")
        elif arguments.command == "list-projects":
            for project in _database(config).list_projects(enabled_only=False):
                print(
                    f"{project.project_id}\t{project.mode.value}\t{project.title}\t{project.root}"
                )
        elif arguments.command == "set-mode":
            mode = ProjectMode(arguments.mode)
            _require_trusted_ack(mode, arguments.acknowledge_full_user_authority)
            _database(config).set_project_mode(arguments.project_id, mode)
            print(f"Project mode changed to {mode.value}.")
        elif arguments.command == "startup":
            if arguments.state == "enable":
                daemon_arguments, ui_arguments = _startup_arguments(arguments.config)
                enable_startup(Path(sys.executable), daemon_arguments, ui_arguments)
                print("Codito daemon and approval tray will start for this user at sign-in.")
            else:
                disable_startup()
                print("Codito sign-in startup was removed.")
        elif arguments.command == "daemon":
            asyncio.run(CoditoDaemon(config).run())
    except AgentError as exc:
        print(f"Codito error [{exc.code}]: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
