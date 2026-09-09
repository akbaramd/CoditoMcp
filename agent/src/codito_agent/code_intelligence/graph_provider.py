"""Graph-backed code intelligence provider.

NullGraphProvider is the default when no graph database is configured.
It returns precision="unavailable" for impact/architecture operations,
but provides heuristic manifest-based dependency scanning and naming-
convention test discovery for related_tests and dependencies respectively.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any
from xml.etree.ElementTree import ParseError

from codito_protocol.code import (
    CodeArchitectureResult,
    CodeDependenciesResult,
    CodeImpactResult,
    CodeRelatedTestsResult,
    DependencyInfo,
    ManifestInfo,
    Precision,
    ProviderKind,
    TestRelation,
)
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from .source import read_source_text
from .types import WorkspaceSnapshot

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class GraphProvider(ABC):
    @abstractmethod
    async def impact(
        self,
        rel: str,
        snapshot: WorkspaceSnapshot,
        max_nodes: int,
        snapshot_id: str,
    ) -> CodeImpactResult: ...

    @abstractmethod
    async def architecture(
        self,
        snapshot: WorkspaceSnapshot,
        max_packages: int,
        snapshot_id: str,
    ) -> CodeArchitectureResult: ...

    @abstractmethod
    async def dependencies(
        self,
        rel: str | None,
        snapshot: WorkspaceSnapshot,
        direction: str,
        max_results: int,
        snapshot_id: str,
    ) -> CodeDependenciesResult: ...

    @abstractmethod
    async def related_tests(
        self,
        rel: str,
        snapshot: WorkspaceSnapshot,
        max_results: int,
        snapshot_id: str,
    ) -> CodeRelatedTestsResult: ...


# ---------------------------------------------------------------------------
# Heuristic helpers (no graph DB required)
# ---------------------------------------------------------------------------

_TEST_DIR_PATTERNS = re.compile(r"(?:^|/)tests?(?:/|$)|(?:^|/)specs?(?:/|$)", re.I)
_TEST_FILE_PATTERNS = re.compile(
    r"(?:^|/)test_[^/]+$|(?:^|/)[^/]+_test\.[^/]+$|(?:^|/)spec\.[^/]+$|(?:^|/)[^/]+\.spec\.[^/]+$|(?:^|/)[^/]+\.test\.[^/]+$",
    re.I,
)


def _is_test_file(rel: str) -> bool:
    return bool(_TEST_DIR_PATTERNS.search(rel) or _TEST_FILE_PATTERNS.search(rel))


def _stem(rel: str) -> str:
    """Return the file stem (no extension, no directory prefix)."""
    name = os.path.basename(rel)
    return os.path.splitext(name)[0].lower()


def _find_related_tests(rel: str, snapshot: WorkspaceSnapshot) -> list[TestRelation]:
    """Heuristic: find test files related to rel by name and path conventions."""
    target_stem = _stem(rel)
    # Strip leading "src/" from path for path-based matching
    rel_clean = re.sub(r"^(?:src|lib|source)/", "", rel, flags=re.I)
    rel_dir = os.path.dirname(rel_clean)
    tests: list[TestRelation] = []

    for candidate in snapshot.file_states:
        if candidate == rel:
            continue
        if not _is_test_file(candidate):
            continue

        cand_stem = _stem(candidate)
        # Exact name match: test_foo.py ↔ foo.py, foo_test.py ↔ foo.py
        name_match = (
            cand_stem == f"test_{target_stem}"
            or cand_stem == f"{target_stem}_test"
            or cand_stem == target_stem
            or cand_stem.endswith(f"_{target_stem}")
            or cand_stem.startswith(f"{target_stem}_")
        )
        if name_match:
            relationship: str
            if cand_stem in {f"test_{target_stem}", f"{target_stem}_test"}:
                relationship = "heuristic_name"
            else:
                relationship = "heuristic_path"
            tests.append(TestRelation(path=candidate, relationship=relationship))
        elif rel_dir and rel_dir in candidate:
            tests.append(TestRelation(path=candidate, relationship="heuristic_path"))

    # De-duplicate preserving order, prefer higher-confidence first
    seen: set[str] = set()
    unique: list[TestRelation] = []
    for t in sorted(tests, key=lambda x: 0 if x.relationship == "heuristic_name" else 1):
        if t.path not in seen:
            seen.add(t.path)
            unique.append(t)
    return unique


def _parse_npm_deps(pkg: dict[str, Any], direction: str) -> list[DependencyInfo]:
    deps: list[DependencyInfo] = []
    sources: list[tuple[dict[str, Any], str]] = []
    if direction in ("all", "direct"):
        sources.append((pkg.get("dependencies", {}), "direct"))
        sources.append((pkg.get("peerDependencies", {}), "direct"))
    if direction in ("all", "direct", "transitive"):
        # devDependencies are considered "dev"
        sources.append((pkg.get("devDependencies", {}), "dev"))
    for mapping, dep_direction in sources:
        for name, version_spec in mapping.items():
            if not isinstance(name, str) or not name:
                continue
            deps.append(
                DependencyInfo(
                    name=name,
                    version=str(version_spec) if version_spec else None,
                    kind="npm",
                    direction=dep_direction,
                )
            )
    return deps


def _parse_pyproject_deps(data: dict[str, Any], direction: str) -> list[DependencyInfo]:
    deps: list[DependencyInfo] = []
    project = data.get("project", {})
    # PEP 621 / uv / poetry
    if direction in ("all", "direct"):
        for dep_str in project.get("dependencies", []):
            if isinstance(dep_str, str):
                # e.g. "requests>=2.0"
                match = re.match(r"^([A-Za-z0-9_.-]+)", dep_str)
                if match:
                    deps.append(
                        DependencyInfo(
                            name=match.group(1), version=dep_str, kind="python", direction="direct"
                        )
                    )
    if direction in ("all", "direct"):
        opt_deps = project.get("optional-dependencies", {})
        for _group, group_deps in opt_deps.items():
            for dep_str in group_deps or []:
                if isinstance(dep_str, str):
                    match = re.match(r"^([A-Za-z0-9_.-]+)", dep_str)
                    if match:
                        deps.append(
                            DependencyInfo(
                                name=match.group(1), version=dep_str, kind="python", direction="dev"
                            )
                        )
    # Poetry
    poetry = data.get("tool", {}).get("poetry", {})
    if poetry and direction in ("all", "direct"):
        for name, spec in poetry.get("dependencies", {}).items():
            if name == "python":
                continue
            deps.append(
                DependencyInfo(
                    name=name,
                    version=str(spec) if spec else None,
                    kind="python",
                    direction="direct",
                )
            )
        for name, spec in poetry.get("dev-dependencies", {}).items():
            deps.append(
                DependencyInfo(
                    name=name, version=str(spec) if spec else None, kind="python", direction="dev"
                )
            )
    return deps


def _parse_cargo_deps(data: dict[str, Any], direction: str) -> list[DependencyInfo]:
    deps: list[DependencyInfo] = []
    if direction in ("all", "direct"):
        for name, spec in data.get("dependencies", {}).items():
            version = spec.get("version") if isinstance(spec, dict) else str(spec)
            deps.append(DependencyInfo(name=name, version=version, kind="rust", direction="direct"))
    if direction in ("all", "direct"):
        for name, spec in data.get("dev-dependencies", {}).items():
            version = spec.get("version") if isinstance(spec, dict) else str(spec)
            deps.append(DependencyInfo(name=name, version=version, kind="rust", direction="dev"))
    return deps


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_dotnet_deps(text: str) -> list[DependencyInfo]:
    deps: list[DependencyInfo] = []
    root = ET.fromstring(text)
    for element in root.iter():
        if _xml_local_name(element.tag) != "PackageReference":
            continue
        name = element.attrib.get("Include") or element.attrib.get("Update")
        if not name:
            continue
        version = element.attrib.get("Version")
        private_assets = ""
        for child in element:
            child_name = _xml_local_name(child.tag)
            if child_name == "Version" and child.text:
                version = child.text.strip()
            elif child_name == "PrivateAssets" and child.text:
                private_assets = child.text.strip().lower()
        deps.append(
            DependencyInfo(
                name=name,
                version=version,
                kind="dotnet",
                direction="dev" if private_assets == "all" else "direct",
            )
        )
    return deps


def _parse_maven_deps(text: str) -> list[DependencyInfo]:
    deps: list[DependencyInfo] = []
    root = ET.fromstring(text)
    for dependency in root.iter():
        if _xml_local_name(dependency.tag) != "dependency":
            continue
        values: dict[str, str] = {}
        for child in dependency:
            if child.text:
                values[_xml_local_name(child.tag)] = child.text.strip()
        artifact = values.get("artifactId")
        if not artifact:
            continue
        group = values.get("groupId")
        deps.append(
            DependencyInfo(
                name=f"{group}:{artifact}" if group else artifact,
                version=values.get("version"),
                kind="maven",
                direction="dev" if values.get("scope", "compile").lower() == "test" else "direct",
            )
        )
    return deps


_GRADLE_DEP_RE = re.compile(
    r"^\s*(implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly)\s*"
    r"(?:\(\s*)?[\"']([^\"']+)[\"']",
    re.MULTILINE,
)


def _parse_gradle_deps(text: str) -> list[DependencyInfo]:
    deps: list[DependencyInfo] = []
    for configuration, coordinate in _GRADLE_DEP_RE.findall(text):
        parts = coordinate.split(":")
        deps.append(
            DependencyInfo(
                name=":".join(parts[:2]) if len(parts) >= 2 else coordinate,
                version=parts[2] if len(parts) >= 3 else None,
                kind="gradle",
                direction="dev" if configuration.startswith("test") else "direct",
            )
        )
    return deps


def _parse_requirements_deps(text: str) -> list[DependencyInfo]:
    deps: list[DependencyInfo] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-", "git+", "http://", "https://")):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)(.*)$", line)
        if match:
            deps.append(
                DependencyInfo(
                    name=match.group(1),
                    version=line,
                    kind="python",
                    direction="direct",
                )
            )
    return deps


def _parse_manifest_dependencies(snapshot: WorkspaceSnapshot) -> list[DependencyInfo]:
    """Parse known manifest files in the snapshot for dependency information."""
    root = snapshot.project_root
    all_deps: list[DependencyInfo] = []

    for rel in snapshot.file_states:
        name = os.path.basename(rel)
        try:
            if name == "package.json":
                text = read_source_text(root, rel, snapshot)
                pkg = json.loads(text)
                all_deps.extend(_parse_npm_deps(pkg, "all"))
            elif name == "pyproject.toml":
                data = tomllib.loads(read_source_text(root, rel, snapshot))
                all_deps.extend(_parse_pyproject_deps(data, "all"))
            elif name == "Cargo.toml":
                data = tomllib.loads(read_source_text(root, rel, snapshot))
                all_deps.extend(_parse_cargo_deps(data, "all"))
            elif name == "go.mod":
                text = read_source_text(root, rel, snapshot)
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("require ") or line.startswith("\t"):
                        parts = line.strip().split()
                        if len(parts) >= 2 and "/" in parts[0]:
                            all_deps.append(
                                DependencyInfo(
                                    name=parts[0],
                                    version=parts[1] if len(parts) > 1 else None,
                                    kind="go",
                                    direction="direct",
                                )
                            )
            elif (
                name.endswith((".csproj", ".fsproj", ".vbproj"))
                or name == "Directory.Packages.props"
            ):
                all_deps.extend(_parse_dotnet_deps(read_source_text(root, rel, snapshot)))
            elif name == "pom.xml":
                all_deps.extend(_parse_maven_deps(read_source_text(root, rel, snapshot)))
            elif name in {"build.gradle", "build.gradle.kts"}:
                all_deps.extend(_parse_gradle_deps(read_source_text(root, rel, snapshot)))
            elif name == "requirements.txt":
                all_deps.extend(_parse_requirements_deps(read_source_text(root, rel, snapshot)))
        except (
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
            tomllib.TOMLDecodeError,
            ParseError,
            DefusedXmlException,
        ) as exc:
            logger.debug("Skipping unreadable manifest %s (%s)", rel, type(exc).__name__)

    # Deduplicate by (name, kind)
    seen: set[tuple[str, str]] = set()
    unique: list[DependencyInfo] = []
    for d in all_deps:
        key = (d.name, d.kind)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


# ---------------------------------------------------------------------------
# NullGraphProvider
# ---------------------------------------------------------------------------


class NullGraphProvider(GraphProvider):
    """Returns precision="unavailable" for graph queries; heuristics for tests/deps."""

    async def impact(
        self, rel: str, snapshot: WorkspaceSnapshot, max_nodes: int, snapshot_id: str
    ) -> CodeImpactResult:
        return CodeImpactResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.NONE,
            precision=Precision.UNAVAILABLE,
            captured_at=_now_iso(),
            warnings=["Graph provider not configured; impact analysis unavailable"],
        )

    async def architecture(
        self, snapshot: WorkspaceSnapshot, max_packages: int, snapshot_id: str
    ) -> CodeArchitectureResult:
        # Build manifest list from snapshot
        manifests: list[ManifestInfo] = []
        manifest_names = {
            "package.json": "npm",
            "pyproject.toml": "python",
            "Cargo.toml": "rust",
            "go.mod": "go",
            "pom.xml": "maven",
            "build.gradle": "gradle",
            "build.gradle.kts": "gradle",
            "requirements.txt": "python",
            "Gemfile": "ruby",
            "composer.json": "php",
        }
        for rel in snapshot.file_states:
            name = os.path.basename(rel)
            kind = manifest_names.get(name)
            if kind:
                manifests.append(ManifestInfo(path=rel, kind=kind))

        return CodeArchitectureResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.MANIFEST,
            precision=Precision.STRUCTURAL,
            captured_at=_now_iso(),
            packages=[],
            manifests=manifests[:128],
            truncated=False,
            warnings=[
                (
                    "Architecture view limited to manifests; configure a graph backend "
                    "for full analysis"
                )
            ],
        )

    async def dependencies(
        self,
        rel: str | None,
        snapshot: WorkspaceSnapshot,
        direction: str,
        max_results: int,
        snapshot_id: str,
    ) -> CodeDependenciesResult:
        all_deps = _parse_manifest_dependencies(snapshot)
        if direction == "direct":
            filtered = [d for d in all_deps if d.direction in ("direct",)]
        elif direction == "transitive":
            filtered = [d for d in all_deps if d.direction == "transitive"]
        else:
            filtered = all_deps
        total = len(filtered)
        return CodeDependenciesResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.MANIFEST,
            precision=Precision.STRUCTURAL,
            captured_at=_now_iso(),
            dependencies=filtered[:max_results],
            truncated=total > max_results,
        )

    async def related_tests(
        self,
        rel: str,
        snapshot: WorkspaceSnapshot,
        max_results: int,
        snapshot_id: str,
    ) -> CodeRelatedTestsResult:
        tests = _find_related_tests(rel, snapshot)
        total = len(tests)
        return CodeRelatedTestsResult(
            snapshot_id=snapshot_id,
            provider=ProviderKind.MANIFEST,
            precision=Precision.STRUCTURAL,
            captured_at=_now_iso(),
            tests=tests[:max_results],
            truncated=total > max_results,
        )
