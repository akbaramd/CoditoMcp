"""Bounded, consented primary-display screenshot contract."""

from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from .types import CoditoModel, Sha256

MAX_IMAGE_BYTES = 600000
PNG_HEADER_BYTES = 33


class DeviceScreenshotInput(CoditoModel):
    purpose: str = Field(min_length=1, max_length=1000)
    display: Literal["primary"] = "primary"
    max_dimension: int = Field(default=1600, ge=640, le=2048)


class DeviceScreenshotResult(CoditoModel):
    mime_type: Literal["image/png"] = "image/png"
    display: Literal["primary"] = "primary"
    width: int = Field(ge=1, le=2048)
    height: int = Field(ge=1, le=2048)
    captured_at: datetime
    sha256: Sha256
    image_base64: str = Field(min_length=1, max_length=800000)

    @model_validator(mode="after")
    def valid_image(self) -> DeviceScreenshotResult:
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
        if self.captured_at.tzinfo is None:
            raise ValueError("Screenshot timestamp requires timezone")
        return self


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
