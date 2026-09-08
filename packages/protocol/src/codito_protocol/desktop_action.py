"""Small, consented desktop actions; this is not remote input automation."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from pydantic import AfterValidator, AnyHttpUrl, Field, TypeAdapter

from .types import CoditoModel

_HTTP_URL = TypeAdapter(AnyHttpUrl)
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")
MAX_BROWSER_URL_LENGTH = 4096


def _browser_url(value: str) -> str:
    """Reject ambiguous inputs before a browser can normalize or interpret them."""
    if (
        not value
        or len(value) > MAX_BROWSER_URL_LENGTH
        or "\\" in value
        or any(
            character.isspace() or unicodedata.category(character)[0] == "C" for character in value
        )
        or _BAD_PERCENT.search(value)
    ):
        raise ValueError("Browser URL contains whitespace, controls, or invalid escaping")
    # Encoded controls can hide a different destination/action in consent UI.
    decoded = unquote(value, errors="strict")
    if any(unicodedata.category(character)[0] == "C" for character in decoded):
        raise ValueError("Browser URL contains encoded controls")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or "%" in parsed.netloc
    ):
        raise ValueError("Only absolute HTTP/HTTPS URLs without credentials are allowed")
    # Reject invalid hosts/ports rather than falling through to OS scheme handlers.
    normalized = _HTTP_URL.validate_python(value)
    return str(normalized)


BrowserUrl = Annotated[
    str, Field(min_length=1, max_length=MAX_BROWSER_URL_LENGTH), AfterValidator(_browser_url)
]
BrowserChoice = Literal["default", "firefox"]


class DeviceDesktopInput(CoditoModel):
    action: Literal["open_browser"] = "open_browser"
    browser: BrowserChoice = "default"
    url: BrowserUrl
    purpose: str = Field(min_length=1, max_length=1000)


class DeviceDesktopResult(CoditoModel):
    action: Literal["open_browser"] = "open_browser"
    browser: BrowserChoice = "default"
    url: BrowserUrl
    status: Literal["submitted"] = "submitted"
