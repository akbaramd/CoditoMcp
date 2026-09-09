from __future__ import annotations

import asyncio
import importlib.util
import struct
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "packaging" / "entrypoints" / "daemon.py"


def _load_entrypoint() -> Any:
    spec = importlib.util.spec_from_file_location("codito_packaged_daemon_test", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@pytest.mark.asyncio
async def test_frozen_browser_smoke_uses_packaged_browser_and_removes_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entrypoint = _load_entrypoint()
    stage_root = tmp_path / "stage"
    daemon_executable = stage_root / "daemon" / "codito-agent-daemon.exe"
    browser_root = stage_root / "browsers"
    browser_root.mkdir(parents=True)
    profile_root = tmp_path / "isolated-smoke-profile"
    launch: dict[str, Any] = {}

    class Page:
        async def goto(self, url: str, **options: Any) -> None:
            assert url.startswith("http://127.0.0.1:")
            assert options == {"wait_until": "domcontentloaded", "timeout": 15_000}

        async def screenshot(self, **options: Any) -> bytes:
            assert options == {"type": "png", "full_page": False}
            return b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 320, 240)

    class CDP:
        async def send(self, method: str) -> dict[str, Any]:
            if method == "Accessibility.enable":
                return {}
            assert method == "Accessibility.getFullAXTree"
            return {
                "nodes": [
                    {
                        "role": {"value": "button"},
                        "name": {"value": entrypoint._SMOKE_NAME},
                    }
                ]
            }

    class Context:
        def __init__(self) -> None:
            self.pages = [Page()]

        async def new_page(self) -> Page:
            raise AssertionError("The persistent context should provide its initial page")

        async def new_cdp_session(self, page: Page) -> CDP:
            assert page is self.pages[0]
            return CDP()

        async def close(self) -> None:
            launch["context_closed"] = True

    class Chromium:
        async def launch_persistent_context(self, profile: str, **options: Any) -> Context:
            await asyncio.to_thread(Path(profile).mkdir, parents=True)
            launch.update(options)
            launch["profile"] = profile
            return Context()

    class Runtime:
        chromium = Chromium()

        async def stop(self) -> None:
            launch["runtime_stopped"] = True

    class Loader:
        async def start(self) -> Runtime:
            return Runtime()

    fake_async_api = types.ModuleType("playwright.async_api")
    fake_async_api.async_playwright = lambda: Loader()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)
    monkeypatch.setattr(entrypoint.sys, "frozen", True, raising=False)
    monkeypatch.setattr(entrypoint.sys, "executable", str(daemon_executable))
    monkeypatch.setattr(entrypoint.tempfile, "mkdtemp", lambda **_options: str(profile_root))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "C:/attacker/browsers")
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "C:/attacker/node.exe")

    await entrypoint._run_packaged_browser_smoke()

    assert entrypoint.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browser_root)
    assert "PLAYWRIGHT_NODEJS_PATH" not in entrypoint.os.environ
    assert launch["profile"] == str(profile_root / "profile")
    assert launch["headless"] is True
    assert launch["channel"] == "chromium"
    assert launch["chromium_sandbox"] is True
    assert launch["viewport"] == {"width": 320, "height": 240}
    assert "executable_path" not in launch
    assert launch["context_closed"] is True
    assert launch["runtime_stopped"] is True
    assert not profile_root.exists()


def test_hidden_browser_smoke_bypasses_daemon_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _load_entrypoint()

    async def smoke() -> None:
        return None

    def unexpected_config_load(_path: Path | None) -> None:
        raise AssertionError("Smoke mode must not load daemon configuration")

    monkeypatch.setattr(entrypoint, "_run_packaged_browser_smoke", smoke)
    monkeypatch.setattr(entrypoint.AgentConfig, "load", unexpected_config_load)
    monkeypatch.setattr(entrypoint.sys, "argv", ["codito-agent-daemon", "--packaged-browser-smoke"])

    entrypoint.main()
