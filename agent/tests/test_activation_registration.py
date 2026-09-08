import io
import sys
from types import SimpleNamespace

import pytest

from codito_agent import notifications


class RegistryKey:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def test_registration_publishes_com_and_legacy_handlers_without_user_choice(tmp_path, monkeypatch):
    tray = tmp_path / "tray" / "codito-agent-tray.exe"
    broker = tmp_path / "broker" / "Codito.Broker.exe"
    tray.parent.mkdir()
    broker.parent.mkdir()
    tray.touch()
    broker.touch()
    entries = {}
    calls = []
    hive = object()

    def create(root, path):
        assert root is hive
        return RegistryKey(path)

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=hive,
        REG_SZ=1,
        REG_DWORD=4,
        CreateKey=create,
        OpenKey=create,
        SetValueEx=lambda key, name, _, kind, value: entries.__setitem__(
            (key.path, name), (value, kind)
        ),
        QueryValueEx=lambda key, name: entries[(key.path, name)],
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(notifications, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        notifications, "_notify_association_changed", lambda: calls.append("notify")
    )
    monkeypatch.setattr(notifications, "event", lambda *_, **__: None)
    monkeypatch.setattr(
        notifications.subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=0)
    )
    assert notifications.register_activation(tray)
    clsid = notifications.TOAST_ACTIVATOR_CLSID
    assert entries[(rf"Software\Classes\CLSID\{clsid}\LocalServer32", "")][0] == (
        f'"{broker}" --toast-server'
    )
    assert entries[(r"Software\Classes\codito-approval\shell\open\command", "")][0] == (
        f'"{tray}" --toast-activation "%1"'
    )
    assert calls == ["notify"]
    assert not any("UserChoice" in path or "Notifications\\Settings" in path for path, _ in entries)
    # Reinstallation/upgrade repairs exactly the application-owned records.
    assert notifications.register_activation(tray)


def test_registration_rejects_non_installed_interpreter(tmp_path, monkeypatch):
    executable = tmp_path / "python.exe"
    executable.touch()
    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace())
    monkeypatch.setattr(notifications, "os", SimpleNamespace(name="nt"))
    with pytest.raises(ValueError, match="installed Codito"):
        notifications.register_activation(executable)


class FakeListener:
    def __init__(self, response=b"ready\r\n"):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(response)
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = 1


def test_listener_is_singleton_and_exits_with_tray(tmp_path, monkeypatch):
    listener = FakeListener()
    spawned = []

    def spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        return listener

    monkeypatch.setattr(notifications, "_listener", None)
    monkeypatch.setattr(notifications.subprocess, "Popen", spawn)
    notifications.ensure_activation_listener(tmp_path / "Codito.Broker.exe")
    notifications.ensure_activation_listener(tmp_path / "Codito.Broker.exe")
    assert len(spawned) == 1
    assert spawned[0][0][0][1] == "--toast-listener"
    assert spawned[0][1]["creationflags"] == 0x08000000
    notifications.stop_activation_listener()
    assert listener.stdin.closed
    assert listener.stdout.closed
    assert not listener.terminated


def test_listener_must_be_ready_before_notification_can_be_shown(tmp_path, monkeypatch):
    listener = FakeListener(b"invalid\n")
    monkeypatch.setattr(notifications, "_listener", None)
    monkeypatch.setattr(notifications.subprocess, "Popen", lambda *_, **__: listener)
    with pytest.raises(ValueError, match="listener unavailable"):
        notifications.ensure_activation_listener(tmp_path / "Codito.Broker.exe")
    assert listener.terminated
    assert notifications._listener is None
