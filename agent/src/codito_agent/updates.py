from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

from . import __version__
from .errors import AgentError

GITHUB_RELEASE_API = "https://api.github.com/repos/akbaramd/CoditoMcp/releases/latest"
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_EXPANDED_BYTES = 1_500 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 12_000
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise AgentError("invalid_release", f"Unsupported release version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class ReleaseUpdate:
    current_version: str
    latest_version: str
    available: bool
    release_url: str
    asset_name: str
    asset_url: str
    asset_sha256: str
    asset_size: int
    published_at: str | None


class GitHubUpdateService:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def check(self) -> ReleaseUpdate:
        client = self._client or httpx.Client(
            timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=False
        )
        try:
            response = client.get(
                GITHUB_RELEASE_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                    "User-Agent": f"Codito-Windows-Agent/{__version__}",
                },
            )
            if response.status_code == 404:
                raise AgentError("release_unavailable", "No stable Codito release is published yet")
            response.raise_for_status()
            release = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentError("update_check_failed", "Could not check GitHub for updates") from exc
        finally:
            if self._client is None:
                client.close()

        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise AgentError("invalid_release", "GitHub returned an invalid stable release")
        latest = str(release.get("tag_name", "")).removeprefix("v")
        latest_tuple = _version_tuple(latest)
        current_tuple = _version_tuple(__version__)
        expected_name = f"Codito-{latest}-win-x64.zip"
        assets = [
            value
            for value in release.get("assets", [])
            if isinstance(value, dict) and value.get("name") == expected_name
        ]
        if len(assets) != 1:
            raise AgentError(
                "invalid_release", f"Release must contain exactly one {expected_name} asset"
            )
        asset = assets[0]
        digest = str(asset.get("digest", ""))
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise AgentError("invalid_release", "Release asset has no trusted SHA-256 digest")
        asset_url = str(asset.get("browser_download_url", ""))
        parsed = httpx.URL(asset_url)
        if parsed.scheme != "https" or parsed.host != "github.com":
            raise AgentError("invalid_release", "Release asset URL is not approved")
        asset_size = int(asset.get("size", 0))
        if not 1 <= asset_size <= MAX_ARCHIVE_BYTES:
            raise AgentError("invalid_release", "Release asset size is outside the allowed range")
        return ReleaseUpdate(
            current_version=__version__,
            latest_version=latest,
            available=latest_tuple > current_tuple,
            release_url=str(release.get("html_url", "")),
            asset_name=expected_name,
            asset_url=asset_url,
            asset_sha256=digest.removeprefix("sha256:"),
            asset_size=asset_size,
            published_at=(str(release["published_at"]) if release.get("published_at") else None),
        )

    def download_and_launch(self, update: ReleaseUpdate) -> Path:
        if not update.available:
            raise AgentError("update_not_required", "Codito is already up to date")
        temp_root = Path(tempfile.mkdtemp(prefix="codito-update-"))
        archive_path = temp_root / update.asset_name
        extract_root = temp_root / "package"
        try:
            self._download(update, archive_path)
            package_root = self._extract(update, archive_path, extract_root)
            installer = package_root / "install-windows.ps1"
            if not installer.is_file():
                raise AgentError("invalid_release", "Release omits install-windows.ps1")
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if not powershell.is_file():
                raise AgentError("update_launch_failed", "Windows PowerShell is unavailable")
            subprocess.Popen(  # noqa: S603 - fixed system executable and validated local script.
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(installer),
                ],
                cwd=package_root,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
            return package_root
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise

    def _download(self, update: ReleaseUpdate, destination: Path) -> None:
        digest = hashlib.sha256()
        total = 0
        with httpx.stream(
            "GET",
            update.asset_url,
            headers={"User-Agent": f"Codito-Windows-Agent/{__version__}"},
            timeout=httpx.Timeout(120.0, connect=15.0),
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            final_host = response.url.host or ""
            if final_host not in {
                "github.com",
                "objects.githubusercontent.com",
                "release-assets.githubusercontent.com",
            }:
                raise AgentError("invalid_release", "Release redirected to an unapproved host")
            with destination.open("xb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES or total > update.asset_size + 1024:
                        raise AgentError("invalid_release", "Release download exceeded its size")
                    digest.update(chunk)
                    output.write(chunk)
        if total != update.asset_size or digest.hexdigest() != update.asset_sha256:
            raise AgentError("invalid_release", "Downloaded release failed its integrity check")

    @staticmethod
    def _extract(update: ReleaseUpdate, archive_path: Path, extract_root: Path) -> Path:
        expected_root = f"Codito-{update.latest_version}-win-x64"
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
                raise AgentError("invalid_release", "Release archive has an invalid entry count")
            total_size = 0
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or "\\" in entry.filename
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != expected_root
                ):
                    raise AgentError("invalid_release", "Release archive contains an unsafe path")
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise AgentError("invalid_release", "Release archive contains a symbolic link")
                total_size += entry.file_size
                if total_size > MAX_EXPANDED_BYTES:
                    raise AgentError("invalid_release", "Release archive expands beyond its limit")
                if entry.compress_size and entry.file_size / entry.compress_size > 250:
                    raise AgentError(
                        "invalid_release", "Release archive has an unsafe compression ratio"
                    )
            extract_root.mkdir()
            archive.extractall(extract_root)
        return extract_root / expected_root
