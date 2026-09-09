from __future__ import annotations

import zipfile
from pathlib import Path

import httpx
import pytest

from codito_agent import __version__
from codito_agent.errors import AgentError
from codito_agent.updates import GitHubUpdateService, ReleaseUpdate


def _release_response(*, digest: str = "sha256:" + "a" * 64) -> dict[str, object]:
    return {
        "tag_name": "v9.9.9",
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.com/akbaramd/CoditoMcp/releases/tag/v9.9.9",
        "published_at": "2026-09-08T00:00:00Z",
        "assets": [
            {
                "name": "Codito-9.9.9-win-x64.zip",
                "browser_download_url": (
                    "https://github.com/akbaramd/CoditoMcp/releases/download/"
                    "v9.9.9/Codito-9.9.9-win-x64.zip"
                ),
                "digest": digest,
                "size": 12345,
            }
        ],
    }


def test_update_check_uses_stable_semver_and_github_digest() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=_release_response())
    )
    with httpx.Client(transport=transport) as client:
        update = GitHubUpdateService(client).check()
    assert update.current_version == __version__
    assert update.latest_version == "9.9.9"
    assert update.available
    assert update.asset_sha256 == "a" * 64


def test_update_check_requires_release_digest() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=_release_response(digest=""))
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(AgentError, match="trusted SHA-256"):
            GitHubUpdateService(client).check()


def test_update_archive_rejects_zip_slip(tmp_path: Path) -> None:
    archive_path = tmp_path / "update.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Codito-0.2.0-win-x64/../../outside.txt", "unsafe")
    update = ReleaseUpdate(
        current_version="0.1.0",
        latest_version="0.2.0",
        available=True,
        release_url="https://github.com/example",
        asset_name=archive_path.name,
        asset_url="https://github.com/example/update.zip",
        asset_sha256="a" * 64,
        asset_size=archive_path.stat().st_size,
        published_at=None,
    )
    with pytest.raises(AgentError, match="unsafe path"):
        GitHubUpdateService._extract(update, archive_path, tmp_path / "expanded")


def test_update_archive_extracts_only_expected_root(tmp_path: Path) -> None:
    archive_path = tmp_path / "update.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Codito-0.2.0-win-x64/install-windows.ps1", "Write-Output ok")
    update = ReleaseUpdate(
        current_version="0.1.0",
        latest_version="0.2.0",
        available=True,
        release_url="https://github.com/example",
        asset_name=archive_path.name,
        asset_url="https://github.com/example/update.zip",
        asset_sha256="a" * 64,
        asset_size=archive_path.stat().st_size,
        published_at=None,
    )
    root = GitHubUpdateService._extract(update, archive_path, tmp_path / "expanded")
    assert (root / "install-windows.ps1").read_text() == "Write-Output ok"
