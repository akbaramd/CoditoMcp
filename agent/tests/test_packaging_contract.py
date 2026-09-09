from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "agent" / "packaging"
WORKFLOWS = ROOT / ".github" / "workflows"


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
    assert "--packaged-browser-smoke" in build_script
    assert "daemon\\_internal\\playwright\\driver\\node.exe" in build_script
    assert "daemon\\_internal\\playwright\\driver\\package\\cli.js" in build_script
    archive_test = (PACKAGING / "scripts" / "Test-WindowsDevPackage.ps1").read_text(
        encoding="utf-8"
    )
    assert "daemon/_internal/playwright/driver/node.exe" in archive_test
    assert "daemon/_internal/playwright/driver/package/cli.js" in archive_test
    daemon_entrypoint = (PACKAGING / "entrypoints" / "daemon.py").read_text(encoding="utf-8")
    assert "help=argparse.SUPPRESS" in daemon_entrypoint
    assert 'os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)' in daemon_entrypoint
    assert 'os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)' in daemon_entrypoint
    assert 'channel="chromium"' in daemon_entrypoint
    assert "chromium_sandbox=True" in daemon_entrypoint
    assert "fallback_channel" not in daemon_entrypoint
    tray_spec = (PACKAGING / "pyinstaller" / "codito-agent-tray.spec").read_text(encoding="utf-8")
    assert 'Path(entry[0]).name.lower() != "icuuc.dll"' in tray_spec
    assert 'startswith("icudt")' in tray_spec


def test_github_actions_are_commit_pinned_and_browser_cache_is_not_reused() -> None:
    for workflow_name in ("windows-package.yml", "windows-release.yml"):
        workflow = (WORKFLOWS / workflow_name).read_text(encoding="utf-8")
        action_refs = re.findall(r"^\s*- uses: ([^\s#]+)", workflow, flags=re.MULTILINE)
        assert action_refs
        assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_refs)
        assert "actions/cache@" not in workflow
        assert "playwright-cache" not in workflow


def test_manual_package_version_is_validated_before_shell_or_artifact_use() -> None:
    workflow = (WORKFLOWS / "windows-package.yml").read_text(encoding="utf-8")
    assert "CODITO_INPUT_VERSION: ${{ inputs.version }}" in workflow
    assert "(?:0|[1-9]\\d*)" in workflow
    assert "-Version '${{ inputs.version }}'" not in workflow
    assert "name: codito-${{ inputs.version }}" not in workflow
    assert workflow.count("-Version $env:CODITO_PACKAGE_VERSION") == 2
    assert "name: codito-${{ steps.version.outputs.value }}-windows-unsigned" in workflow


def test_release_workflow_separates_verification_from_native_publish() -> None:
    workflow = (WORKFLOWS / "windows-release.yml").read_text(encoding="utf-8")
    build_job, publish_job = workflow.split("  native-publish:", maxsplit=1)
    assert "permissions:\n  contents: read" in workflow
    assert "contents: write" not in build_job
    assert "GH_TOKEN" not in build_job
    assert "contents: write" in publish_job
    assert workflow.count("persist-credentials: false") == 2
    assert "actions/upload-artifact@" in build_job
    assert "actions/download-artifact@" in publish_job
    assert "release-metadata.json" in workflow
    assert "git push --atomic" in publish_job
    assert '"${tagRef}:${tagRef}"' in publish_job
    assert '"refs/tags/$env:CODITO_RELEASE_TAG:refs/tags/' not in publish_job


def test_release_retry_is_bound_to_exact_tag_tree_and_verified_assets() -> None:
    workflow = (WORKFLOWS / "windows-release.yml").read_text(encoding="utf-8")
    build_job, _ = workflow.split("  native-publish:", maxsplit=1)
    assert "$stablePattern = '\\Av(?<version>" in workflow
    assert "git tag --list 'v[0-9]*.[0-9]*.[0-9]*'" not in workflow
    assert "if ($tag -in $rawTags)" in build_job
    assert 'git show-ref --verify --quiet "refs/tags/$tag"' not in build_job
    assert "$parentFields.Count -eq 2" in workflow
    assert "^{tree}" in workflow
    assert "--clobber" in workflow
    assert "$assets.Count -ne 4" in workflow
    assert "Published release asset names are incomplete or contain extras." in workflow
    assert '"sha256:$($localDigests[$asset.name])"' in workflow


def test_browser_is_downloaded_into_a_fresh_non_overridable_directory() -> None:
    build_script = (PACKAGING / "scripts" / "Build-WindowsDevPackage.ps1").read_text(
        encoding="utf-8"
    )
    assert "PlaywrightBrowserCache" not in build_script
    assert "$playwrightBrowserRoot = Join-Path $outputRoot 'playwright-browser'" in build_script
    assert "Reset-RepositoryDirectory -Path $playwrightBrowserRoot" in build_script
    for variable in (
        "PLAYWRIGHT_NODEJS_PATH",
        "PLAYWRIGHT_DOWNLOAD_HOST",
        "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
        "NODE_OPTIONS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "NODE_EXTRA_CA_CERTS",
    ):
        assert f"$env:{variable} = $null" in build_script
    assert '"chromium-$chromiumRevision"' in build_script
    assert "chrome-win64\\chrome.exe" in build_script


def test_installer_requires_complete_manifest_and_one_browser_revision() -> None:
    installer = (PACKAGING / "scripts" / "Install-WindowsDevPackage.ps1").read_text(
        encoding="utf-8"
    )
    assert "daemon/_internal/playwright/driver/node.exe" in installer
    assert "daemon/_internal/playwright/driver/package/cli.js" in installer
    assert "FileAttributes]::ReparsePoint" in installer
    assert "SHA256SUMS contains a duplicate path" in installer
    assert "$manifestEntries.Count -ne $physicalFiles.Count" in installer
    assert "Package manifest omits physical file" in installer
    assert "Package manifest names a file that is not present" in installer
    assert "^browsers/chromium-(?<revision>\\d+)/chrome-win64/chrome\\.exe$" in installer
    assert "Package browser entry is outside the pinned Chromium revision" in installer


@pytest.mark.skipif(sys.platform != "win32", reason="the package installer is Windows-only")
@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing-driver", "Package manifest omits required file"),
        ("unmanifested-extra", "does not describe every package file exactly once"),
        ("duplicate-manifest", "SHA256SUMS contains a duplicate path"),
        ("duplicate-browser", "exactly one pinned Chromium executable"),
        ("foreign-browser-revision", "outside the pinned Chromium revision"),
        ("extra-manifest", "does not describe every package file exactly once"),
    ],
)
def test_installer_rejects_incomplete_or_ambiguous_payloads(
    tmp_path: Path, mutation: str, expected_error: str
) -> None:
    payload = {
        "VERSION": b"0.3.0\n",
        "cli/codito-agent.exe": b"cli",
        "daemon/codito-agent-daemon.exe": b"daemon",
        "tray/codito-agent-tray.exe": b"tray",
        "broker/Codito.Broker.exe": b"broker",
        "daemon/_internal/playwright/driver/node.exe": b"node",
        "daemon/_internal/playwright/driver/package/cli.js": b"cli-js",
        "browsers/chromium-123/chrome-win64/chrome.exe": b"chromium",
    }
    if mutation == "missing-driver":
        del payload["daemon/_internal/playwright/driver/node.exe"]
    elif mutation == "duplicate-browser":
        payload["browsers/chromium-123/alternate/chrome.exe"] = b"second-chromium"
    elif mutation == "foreign-browser-revision":
        payload["browsers/chromium-999/resources.pak"] = b"foreign-revision"

    for relative, contents in payload.items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)

    manifest_lines = [
        f"{hashlib.sha256(contents).hexdigest()}  {relative}"
        for relative, contents in sorted(payload.items())
    ]
    if mutation == "duplicate-manifest":
        manifest_lines.append(manifest_lines[0])
    elif mutation == "extra-manifest":
        manifest_lines.append(f"{'0' * 64}  absent.bin")
    (tmp_path / "SHA256SUMS").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    if mutation == "unmanifested-extra":
        (tmp_path / "unmanifested.bin").write_bytes(b"extra")

    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell is not None
    result = subprocess.run(  # noqa: S603 - executable is resolved from the trusted PATH
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(PACKAGING / "scripts" / "Install-WindowsDevPackage.ps1"),
            "-PackageRoot",
            str(tmp_path),
            "-NoStart",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert expected_error in result.stdout + result.stderr
