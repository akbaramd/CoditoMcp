"""Windowless packaged entry point for the per-user Codito daemon."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

from codito_agent.config import AgentConfig
from codito_agent.daemon import CoditoDaemon
from codito_agent.errors import AgentError

_SMOKE_NAME = "Codito packaged browser smoke"
_SMOKE_HTML = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{_SMOKE_NAME}</title></head>
<body><main><button id="codito-smoke" type="button">{_SMOKE_NAME}</button></main></body>
</html>
""".encode()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codito-agent-daemon")
    parser.add_argument("--config", type=Path, help="Path to agent TOML configuration")
    parser.add_argument(
        "--packaged-browser-smoke",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _packaged_broker() -> Path:
    # The release layout places daemon/ and broker/ next to one another. Using
    # sys.executable (instead of PyInstaller's extraction directory) works for
    # both onedir builds and the installed MSI payload.
    return Path(sys.executable).resolve().parent.parent / "broker" / "Codito.Broker.exe"


def _packaged_browsers() -> Path:
    return Path(sys.executable).resolve().parent.parent / "browsers"


async def _serve_smoke_page(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        request_line = request.split(b"\r\n", 1)[0]
        if request_line == b"GET / HTTP/1.1":
            status = b"HTTP/1.1 200 OK"
            body = _SMOKE_HTML
            content_type = b"text/html; charset=utf-8"
        else:
            status = b"HTTP/1.1 404 Not Found"
            body = b"not found"
            content_type = b"text/plain; charset=utf-8"
        writer.write(
            status
            + b"\r\nContent-Type: "
            + content_type
            + b"\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n"
            + body
        )
        await writer.drain()
    except (asyncio.IncompleteReadError, TimeoutError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


def _is_expected_semantic_button(tree: dict[str, Any]) -> bool:
    for node in tree.get("nodes", []):
        if not isinstance(node, dict):
            continue
        role = node.get("role") if isinstance(node.get("role"), dict) else {}
        name = node.get("name") if isinstance(node.get("name"), dict) else {}
        if role.get("value") == "button" and name.get("value") == _SMOKE_NAME:
            return True
    return False


async def _run_packaged_browser_smoke() -> None:
    """Exercise the frozen Playwright driver and browser without agent configuration."""

    if not getattr(sys, "frozen", False):
        raise RuntimeError("Packaged browser smoke mode requires a frozen daemon")
    browser_root = _packaged_browsers()
    if not browser_root.is_dir():
        raise RuntimeError("Packaged browser directory is missing")
    # Never honor an inherited machine-wide browser cache in the frozen smoke
    # path. Playwright does not download browsers during launch, and no channel
    # fallback is supplied below.
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
    os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)

    profile_root = Path(tempfile.mkdtemp(prefix="codito-packaged-browser-smoke-"))
    server: asyncio.AbstractServer | None = None
    runtime: Any | None = None
    context: Any | None = None
    smoke_error: BaseException | None = None
    try:
        from playwright.async_api import async_playwright

        server = await asyncio.start_server(_serve_smoke_page, host="127.0.0.1", port=0)
        sockets = server.sockets
        if not sockets:
            raise RuntimeError("Loopback smoke server did not bind")
        port = int(sockets[0].getsockname()[1])

        runtime = await async_playwright().start()
        context = await runtime.chromium.launch_persistent_context(
            str(profile_root / "profile"),
            headless=True,
            channel="chromium",
            chromium_sandbox=True,
            viewport={"width": 320, "height": 240},
            device_scale_factor=1,
            accept_downloads=False,
        )
        pages = list(context.pages)
        page = pages[0] if pages else await context.new_page()
        await page.goto(
            f"http://127.0.0.1:{port}/",
            wait_until="domcontentloaded",
            timeout=15_000,
        )
        png = await page.screenshot(type="png", full_page=False)
        if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError("Packaged browser did not return a PNG screenshot")
        if struct.unpack(">II", png[16:24]) != (320, 240):
            raise RuntimeError("Packaged browser returned the wrong screenshot dimensions")

        cdp = await context.new_cdp_session(page)
        await cdp.send("Accessibility.enable")
        tree = await cdp.send("Accessibility.getFullAXTree")
        if not _is_expected_semantic_button(tree):
            raise RuntimeError("Packaged browser did not expose the semantic smoke element")
    except BaseException as exc:
        smoke_error = exc
    finally:
        if context is not None:
            try:
                await context.close()
            except BaseException as exc:
                smoke_error = smoke_error or exc
        if runtime is not None:
            try:
                await runtime.stop()
            except BaseException as exc:
                smoke_error = smoke_error or exc
        if server is not None:
            server.close()
            await server.wait_closed()
        try:
            await asyncio.to_thread(shutil.rmtree, profile_root)
        except BaseException as exc:
            smoke_error = smoke_error or exc
        profile_remains = await asyncio.to_thread(profile_root.exists)
        if profile_remains and smoke_error is None:
            smoke_error = RuntimeError("Packaged browser smoke profile was not removed")
    if smoke_error is not None:
        raise RuntimeError("Packaged browser smoke test failed") from smoke_error


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.packaged_browser_smoke:
        try:
            asyncio.run(_run_packaged_browser_smoke())
        except BaseException as exc:
            raise SystemExit(3) from exc
        return
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
