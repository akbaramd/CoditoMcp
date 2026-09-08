"""Primitive, security-sensitive types used by Codito wire contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

PROTOCOL_VERSION = "1.0"
MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_SCRIPT_BYTES = 256 * 1024
MAX_TOOL_TEXT_BYTES = 10 * 1024 * 1024
MAX_RELATIVE_PATH_CHARS = 1024
MAX_GLOB_CHARS = 512

_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class CoditoModel(BaseModel):
    """Strict base model: unknown fields never silently influence policy."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        str_strip_whitespace=False,
    )


def _validate_opaque_id(value: str) -> str:
    if not _OPAQUE_ID_RE.fullmatch(value):
        raise ValueError("must be an opaque URL-safe identifier (16-128 characters)")
    return value


def _validate_sha256(value: str) -> str:
    normalized = value.lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


def _validate_relative_path(value: str) -> str:
    """Apply portable lexical checks; the Windows agent still validates handles."""

    if value in ("", "."):
        return ""
    if len(value) > MAX_RELATIVE_PATH_CHARS:
        raise ValueError("path is too long")
    if "\x00" in value or "\\" in value:
        raise ValueError("path must use forward slashes and contain no NUL")
    if value.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", value):
        raise ValueError("path must be project-relative")
    if ":" in value:
        raise ValueError("alternate data streams and device paths are forbidden")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("path contains an empty, current, or parent component")
    path = PurePosixPath(value)
    for part in path.parts:
        if part.endswith((".", " ")):
            raise ValueError("trailing dot/space aliases are forbidden")
        basename = part.split(".", 1)[0].upper()
        if basename in _RESERVED_NAMES:
            raise ValueError("reserved Windows path component")
    return path.as_posix()


def _validate_glob(value: str) -> str:
    if len(value) > MAX_GLOB_CHARS or "\x00" in value or "\\" in value or ":" in value:
        raise ValueError("invalid project-relative glob")
    if value.startswith("/"):
        raise ValueError("glob must be project-relative")
    literal_parts = value.replace("**", "*").split("/")
    if any(part in ("", ".", "..") for part in literal_parts):
        raise ValueError("glob contains an invalid component")
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value.astimezone(UTC)


OpaqueId = Annotated[str, AfterValidator(_validate_opaque_id)]
Sha256 = Annotated[str, AfterValidator(_validate_sha256)]
RelativePath = Annotated[str, AfterValidator(_validate_relative_path)]
ProjectGlob = Annotated[str, AfterValidator(_validate_glob)]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
JsonObject = dict[str, Any]
