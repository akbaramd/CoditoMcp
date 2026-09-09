"""Bounded, consented selected-display screenshot contract."""

from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .types import CoditoModel, OpaqueId, Sha256

MAX_IMAGE_BYTES = 600000
PNG_HEADER_BYTES = 33
DisplaySelector = Annotated[str, Field(pattern=r"^(primary|screen_[a-f0-9]{64})$")]
DisplayId = Annotated[str, Field(pattern=r"^screen_[a-f0-9]{64}$")]


class DeviceScreenshotInput(CoditoModel):
    project_id: OpaqueId | None = Field(
        default=None, description="Optional origin project binding."
    )
    action: Literal["capture", "list_displays"] = "capture"
    purpose: str = Field(min_length=1, max_length=1000)
    display: DisplaySelector = "primary"
    max_dimension: int = Field(default=1600, ge=640, le=2048)


class DisplayInfo(CoditoModel):
    id: DisplayId
    label: str = Field(min_length=1, max_length=160)
    primary: bool
    width: int = Field(ge=1, le=32768)
    height: int = Field(ge=1, le=32768)
    scale_factor: float = Field(ge=0.25, le=8)
    identity: Sha256
    persistent_permission_supported: bool


class DisplayCatalog(CoditoModel):
    displays: list[DisplayInfo] = Field(min_length=1, max_length=16)
    topology_id: Sha256

    @model_validator(mode="after")
    def unambiguous_displays(self) -> DisplayCatalog:
        if len({item.id for item in self.displays}) != len(self.displays):
            raise ValueError("Display identities must be unique")
        if sum(item.primary for item in self.displays) != 1:
            raise ValueError("Exactly one primary display is required")
        return self


class DeviceDisplaysResult(DisplayCatalog):
    action: Literal["list_displays"] = "list_displays"


class PngImageMetadata(CoditoModel):
    """Bounded PNG metadata shared by display and managed-page captures."""

    mime_type: Literal["image/png"] = "image/png"
    width: int = Field(ge=1, le=2048)
    height: int = Field(ge=1, le=2048)
    captured_at: datetime
    sha256: Sha256

    @model_validator(mode="after")
    def valid_timestamp(self) -> PngImageMetadata:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("PNG timestamp requires timezone")
        return self


class PngImageResult(PngImageMetadata):
    """Inline PNG bytes plus integrity metadata for transient MCP image delivery."""

    image_base64: str = Field(min_length=1, max_length=800000)

    @model_validator(mode="after")
    def valid_image(self) -> PngImageResult:
        try:
            raw = base64.b64decode(self.image_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid screenshot encoding") from exc
        if (
            len(raw) > MAX_IMAGE_BYTES
            or len(raw) < PNG_HEADER_BYTES
            or raw[:8] != b"\x89PNG\r\n\x1a\n"
            or raw[12:16] != b"IHDR"
            or hashlib.sha256(raw).hexdigest() != self.sha256
            or int.from_bytes(raw[16:20], "big") != self.width
            or int.from_bytes(raw[20:24], "big") != self.height
        ):
            raise ValueError("Invalid screenshot dimensions, type, size or digest")
        return self


class DeviceScreenshotResult(PngImageResult):
    display: DisplaySelector = "primary"


ScreenshotToolResult = DeviceScreenshotResult | DeviceDisplaysResult


def durable_tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep screenshot bytes out of SQLite/PostgreSQL and crash replay journals."""
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("image_base64"), str):
        return payload
    text = "Screenshot bytes are not retained in the durable journal; request a new capture."
    return {
        "ok": False,
        "text": text,
        "error": {
            "code": "outcome_unknown",
            "message": text,
            "retryable": True,
            "details": {
                "image_digest": hashlib.sha256(result["image_base64"].encode()).hexdigest()
            },
        },
    }
