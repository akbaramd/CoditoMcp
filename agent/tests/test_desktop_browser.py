"""No real browser is opened by these tests."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from codito_agent import desktop_browser


@pytest.mark.parametrize("browser", ["default", "firefox"])
def test_locked_desktop_never_opens_browser(monkeypatch, browser):
    monkeypatch.setattr(desktop_browser, "_desktop_unlocked", lambda: False)
    monkeypatch.setattr(
        desktop_browser, "_open_default", lambda value: pytest.fail("Opened browser")
    )
    monkeypatch.setattr(
        desktop_browser, "_firefox_executable", lambda: pytest.fail("Resolved browser")
    )
    assert not desktop_browser.open_browser_url(browser, "https://example.com/")


def test_browser_helper_revalidates_input_before_using_os(monkeypatch):
    monkeypatch.setattr(
        desktop_browser, "_desktop_unlocked", lambda: pytest.fail("Accessed desktop")
    )
    assert not desktop_browser.open_browser_url("default", "file:///C:/Windows/cmd.exe")
    assert not desktop_browser.open_browser_url("cmd", "https://example.com/")


@pytest.mark.parametrize("accepted", [False, True])
def test_default_browser_returns_only_os_acceptance(monkeypatch, accepted):
    received = []
    monkeypatch.setattr(desktop_browser, "_desktop_unlocked", lambda: True)

    def open_default(url):
        received.append(url)
        return accepted

    monkeypatch.setattr(desktop_browser, "_open_default", open_default)
    assert desktop_browser.open_browser_url("default", "HTTPS://EXAMPLE.COM") is accepted
    assert received == ["https://example.com/"]


def test_firefox_uses_fixed_executable_arguments_without_shell_or_agent_secrets(
    monkeypatch, tmp_path
):
    executable = tmp_path / "firefox.exe"
    received = []
    monkeypatch.setattr(desktop_browser, "_desktop_unlocked", lambda: True)
    monkeypatch.setattr(desktop_browser, "_firefox_executable", lambda: executable)
    monkeypatch.setenv("CODITO_DEVICE_TOKEN", "never-pass-to-browser")
    monkeypatch.setenv("PYTHONPATH", "never-pass-to-browser")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "never-pass-to-browser")

    def popen(args, **kwargs):
        received.append((args, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert desktop_browser.open_browser_url("firefox", "https://example.com/?q=%26whoami")
    args, kwargs = received[0]
    assert args == [str(executable), "-new-tab", "https://example.com/?q=%26whoami"]
    assert kwargs["executable"] == str(executable)
    assert kwargs["cwd"] == str(executable.parent)
    assert kwargs["shell"] is False and kwargs["close_fds"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
    assert (
        not {"CODITO_DEVICE_TOKEN", "PYTHONPATH", "_PYI_APPLICATION_HOME_DIR"}
        & kwargs["env"].keys()
    )


def test_firefox_unavailable_or_launch_failure_does_not_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_browser, "_desktop_unlocked", lambda: True)
    monkeypatch.setattr(desktop_browser, "_open_default", lambda value: pytest.fail("Fallback"))
    monkeypatch.setattr(desktop_browser, "_firefox_executable", lambda: None)
    assert not desktop_browser.open_browser_url("firefox", "https://example.com/")
    monkeypatch.setattr(desktop_browser, "_firefox_executable", lambda: tmp_path / "firefox.exe")

    def launch_error(*args, **kwargs):
        raise OSError("Synthetic launch refusal")

    monkeypatch.setattr(subprocess, "Popen", launch_error)
    assert not desktop_browser.open_browser_url("firefox", "https://example.com/")


@pytest.mark.skipif(os.name != "nt", reason="Windows App Paths registration")
def test_firefox_registration_resolves_only_a_fixed_absolute_executable(monkeypatch, tmp_path):
    import winreg

    executable = tmp_path / "firefox.exe"
    executable.write_bytes(b"synthetic executable; never run")

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(winreg, "OpenKey", lambda *args: Key())
    monkeypatch.setattr(winreg, "QueryValueEx", lambda *args: (str(executable), winreg.REG_SZ))
    assert desktop_browser._firefox_executable() == executable
    for raw in (
        "firefox.exe",
        str(executable) + " -profile sensitive",
        "C:\\Windows\\System32\\cmd.exe",
        "\\\\server\\share\\firefox.exe",
    ):
        monkeypatch.setattr(winreg, "QueryValueEx", lambda *args, raw=raw: (raw, winreg.REG_SZ))
        assert desktop_browser._firefox_executable() is None
