from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "agent" / "packaging"


def test_security_sensitive_packaging_versions_are_exactly_pinned() -> None:
    project = tomllib.loads((ROOT / "agent" / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["optional-dependencies"]["package"] == ["pyinstaller==6.22.2"]
    wix_project = (PACKAGING / "wix" / "Codito.Agent.wixproj").read_text(encoding="utf-8")
    assert 'Project Sdk="WixToolset.Sdk/6.0.2"' in wix_project


def test_installer_is_per_user_and_registers_both_startup_processes() -> None:
    source = (PACKAGING / "wix" / "Package.wxs").read_text(encoding="utf-8")
    assert 'Scope="perUser"' in source
    assert "Software\\Microsoft\\Windows\\CurrentVersion\\Run" in source
    assert "Codito Agent Daemon" in source
    assert "Codito Agent Tray" in source
    assert "codito-agent-daemon.exe" in source
    assert "codito-agent-tray.exe" in source
    assert "RemoveFile" not in source


def test_packaging_never_accepts_wix_terms_implicitly() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in PACKAGING.rglob("*") if path.is_file()
    ).lower()
    forbidden_tokens = ("accept" + "eula", "accept" + "-eula")
    assert all(token not in sources for token in forbidden_tokens)


def test_daemon_and_tray_are_windowless_but_cli_has_console() -> None:
    specs = {
        path.name: path.read_text(encoding="utf-8")
        for path in (PACKAGING / "pyinstaller").glob("*.spec")
    }
    assert "console=True" in specs["codito-agent.spec"]
    assert "console=False" in specs["codito-agent-daemon.spec"]
    assert "console=False" in specs["codito-agent-tray.spec"]


def test_release_scripts_include_archive_verification_and_serial_broker_publish() -> None:
    build_script = (PACKAGING / "scripts" / "Build-WindowsDevPackage.ps1").read_text(
        encoding="utf-8"
    )
    assert "Test-WindowsDevPackage.ps1" in build_script
    assert "--maxcpucount:1" in build_script
    tray_spec = (PACKAGING / "pyinstaller" / "codito-agent-tray.spec").read_text(encoding="utf-8")
    assert 'Path(entry[0]).name.lower() != "icuuc.dll"' in tray_spec
    assert 'startswith("icudt")' in tray_spec
