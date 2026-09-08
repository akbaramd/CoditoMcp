from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

TARGETS = {
    "pyproject.toml": (r'(?m)^version = "[^"]+"$', 'version = "{version}"'),
    "agent/pyproject.toml": (r'(?m)^version = "[^"]+"$', 'version = "{version}"'),
    "relay/pyproject.toml": (r'(?m)^version = "[^"]+"$', 'version = "{version}"'),
    "packages/protocol/pyproject.toml": (
        r'(?m)^version = "[^"]+"$',
        'version = "{version}"',
    ),
    "agent/src/codito_agent/__init__.py": (
        r'(?m)^__version__ = "[^"]+"$',
        '__version__ = "{version}"',
    ),
    "relay/codito_relay/__init__.py": (
        r'(?m)^__version__ = "[^"]+"$',
        '__version__ = "{version}"',
    ),
}


def set_version(version: str) -> None:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must use X.Y.Z SemVer")
    for relative, (pattern, replacement) in TARGETS.items():
        path = ROOT / relative
        original = path.read_text(encoding="utf-8")
        updated, count = re.subn(pattern, replacement.format(version=version), original, count=1)
        if count != 1:
            raise RuntimeError(f"expected exactly one version field in {relative}")
        path.write_text(updated, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize the Codito SemVer")
    parser.add_argument("version")
    args = parser.parse_args()
    set_version(args.version)


if __name__ == "__main__":
    main()
