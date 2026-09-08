"""Explicit device reads outside projects. Authority comes only from Windows consent."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .types import (
    MAX_RELATIVE_PATH_CHARS,
    CoditoModel,
    RelativePath,
    Sha256,
    _validate_relative_path,
)

_FIRST_PRINTABLE = 32


def _check_names(value: str) -> None:
    if any(ord(c) < _FIRST_PRINTABLE or c in '<>"|?*' for c in value):
        raise ValueError("invalid Windows directory name")
    if any(re.match(r"^(COM|LPT)[¹²³](\.|$)", part, re.IGNORECASE) for part in value.split("/")):
        raise ValueError("reserved Windows device name")


def normalize_read_scope(value: str) -> str:
    value = value.replace("\\", "/")
    if not re.match(r"^[A-Za-z]:/", value) or len(value) > MAX_RELATIVE_PATH_CHARS:
        raise ValueError("scope_path must be a fully qualified local Windows directory")
    tail = value[3:]
    if tail.endswith("/"):
        tail = tail[:-1]
    if tail == ".":
        raise ValueError("dot components are forbidden")
    _validate_relative_path(tail)
    _check_names(tail)
    return value[0].upper() + ":/" + tail


class DeviceReadInput(CoditoModel):
    operation: Literal["list_directory", "read_file"]
    scope_path: str = Field(description="Directory to request read access to, e.g. C:/")
    path: RelativePath = Field(default="", description="Path relative to scope_path")
    purpose: str = Field(min_length=1, max_length=1000)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=100, ge=1, le=500)
    start_line: int = Field(default=1, ge=1, le=1000000)
    max_lines: int = Field(default=200, ge=1, le=2000)
    max_bytes: int = Field(default=65536, ge=256, le=262144)

    _scope = field_validator("scope_path")(normalize_read_scope)

    @model_validator(mode="after")
    def file_requires_path(self) -> DeviceReadInput:
        if self.operation == "read_file" and not self.path:
            raise ValueError("read_file requires a nonempty relative path")
        _check_names(self.path)
        return self


class DeviceReadEntry(CoditoModel):
    name: str
    kind: Literal["file", "directory", "blocked"]
    size_bytes: int | None = Field(default=None, ge=0)


class DeviceReadResult(CoditoModel):
    operation: Literal["list_directory", "read_file"]
    scope_path: str
    path: RelativePath
    entries: list[DeviceReadEntry] = Field(default_factory=list, max_length=500)
    numbered_text: str = ""
    encoding: str | None = None
    newline: Literal["lf", "crlf", "cr", "mixed", "none"] | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: Sha256 | None = None
    truncated: bool = False
    next_offset: int | None = Field(default=None, ge=0)
    next_line: int | None = Field(default=None, ge=1)
