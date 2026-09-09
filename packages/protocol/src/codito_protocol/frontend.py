"""Managed local-browser contracts for deterministic frontend inspection.

The browser and development server live on the Windows agent. Callers address
elements only through an opaque registry produced by a specific snapshot; raw
selectors, JavaScript, file uploads, and password-entry capabilities are not part
of this protocol.
"""

from __future__ import annotations

from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .screenshot import PngImageMetadata, PngImageResult
from .types import CoditoModel, OpaqueId, RelativePath

FrontendElementId = Annotated[str, Field(pattern=r"^e[1-9][0-9]{0,7}$")]
FrontendText = Annotated[str, Field(max_length=16_384)]
FrontendCssText = Annotated[str, Field(max_length=2_048)]
MAX_FRONTEND_URL_CHARS = 2_048
MAX_FRONTEND_CLASS_CHARS = 256
FIRST_PRINTABLE = 32
DELETE_CONTROL = 127
# JSON Schema uses ECMAScript-style regular expressions, where ``$`` can also
# match immediately before a final newline. The final negative lookahead below
# is an absolute end-of-input assertion that works in both ECMAScript and the
# Python validator used by our schema tests. Runtime validation remains the
# authority and independently rejects every control character.
FRONTEND_ROUTE_PATTERN = r"^/(?!/)[^\\?#\x00-\x1f\x7f]{0,2047}(?![\s\S])"
_IPV6_LOOPBACK_SCHEMA_PATTERN = (
    r"\[(?:(?:0{1,4}:){7}0{0,3}1|"
    r"(?:0{1,4}(?::0{1,4}){0,5})?::0{0,3}1)\]"
)
_LOOPBACK_HOST_SCHEMA_PATTERN = (
    r"(?:[Ll][Oo][Cc][Aa][Ll][Hh][Oo][Ss][Tt]\.?|"
    r"127(?:\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}|"
    rf"{_IPV6_LOOPBACK_SCHEMA_PATTERN})"
)
_PORT_SCHEMA_PATTERN = (
    r"(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|"
    r"65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])"
)
FRONTEND_MANAGED_URL_PATTERN = (
    rf"^http://{_LOOPBACK_HOST_SCHEMA_PATTERN}(?::{_PORT_SCHEMA_PATTERN})?"
    r"(?:/[^\\?#\x00-\x1f\x7f]*)?(?![\s\S])"
)
FRONTEND_MANAGED_ORIGIN_PATTERN = (
    rf"^http://{_LOOPBACK_HOST_SCHEMA_PATTERN}(?::{_PORT_SCHEMA_PATTERN})?/?(?![\s\S])"
)


def validate_frontend_route(value: str) -> str:
    """Accept one app-relative route, never an absolute/network URL."""
    if (
        not value.startswith("/")
        or value.startswith("//")
        or len(value) > MAX_FRONTEND_URL_CHARS
        or "\\" in value
        or any(
            ord(character) < FIRST_PRINTABLE or ord(character) == DELETE_CONTROL
            for character in value
        )
    ):
        raise ValueError("route must be a bounded app-relative route beginning with /")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.username or parsed.password:
        raise ValueError("route cannot contain an origin or credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("route cannot contain a query or fragment; navigate through the UI")
    return value


def _validate_managed_url(value: str) -> str:
    """Accept a sanitized loopback page URL with no credential-bearing suffix."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("managed frontend URL has an invalid host or port") from exc
    host = parsed.hostname or ""
    expected_netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        expected_netloc = f"{expected_netloc}:{port}"
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or port == 0
        or parsed.netloc.casefold() != expected_netloc.casefold()
        or "\\" in value
        or parsed.username is not None
        or parsed.password is not None
        or "%" in host
        or host.endswith("..")
        or len(value) > MAX_FRONTEND_URL_CHARS
        or any(
            ord(character) < FIRST_PRINTABLE or ord(character) == DELETE_CONTROL
            for character in value
        )
    ):
        raise ValueError("managed frontend URL must be bounded HTTP without credentials")
    host = parsed.hostname.lower().removesuffix(".")
    if host != "localhost":
        try:
            loopback = ip_address(host).is_loopback
        except ValueError as exc:
            raise ValueError("managed frontend URL must use a loopback host") from exc
        if not loopback:
            raise ValueError("managed frontend URL must use a loopback host")
    if parsed.query or parsed.fragment:
        raise ValueError("managed frontend URL query and fragment must be redacted")
    return value


def _validate_managed_origin(value: str) -> str:
    """Accept a sanitized loopback origin, optionally with one trailing slash."""
    value = _validate_managed_url(value)
    if urlsplit(value).path not in {"", "/"}:
        raise ValueError("managed frontend base URL must be a bare origin")
    return value


class FrontendAction(StrEnum):
    CLICK = "click"
    HOVER = "hover"
    FOCUS = "focus"
    FILL = "fill"
    PRESS = "press"
    SCROLL = "scroll"
    SELECT = "select"


class FrontendKey(StrEnum):
    ENTER = "Enter"
    TAB = "Tab"
    ESCAPE = "Escape"
    SPACE = "Space"
    ARROW_UP = "ArrowUp"
    ARROW_DOWN = "ArrowDown"
    ARROW_LEFT = "ArrowLeft"
    ARROW_RIGHT = "ArrowRight"
    HOME = "Home"
    END = "End"
    PAGE_UP = "PageUp"
    PAGE_DOWN = "PageDown"
    BACKSPACE = "Backspace"
    DELETE = "Delete"


class FrontendViewport(CoditoModel):
    width: int = Field(default=1440, ge=320, le=2048)
    height: int = Field(default=900, ge=240, le=2048)


class FrontendRect(CoditoModel):
    x: float = Field(ge=-100_000, le=100_000)
    y: float = Field(ge=-100_000, le=100_000)
    width: float = Field(ge=0, le=100_000)
    height: float = Field(ge=0, le=100_000)


class FrontendSourceLocation(CoditoModel):
    """One project-relative source location using 1-based coordinates."""

    path: RelativePath
    line: int = Field(ge=1, le=10_000_000)
    character: int = Field(default=1, ge=1, le=100_000)

    @field_validator("path")
    @classmethod
    def nonempty_source_path(cls, value: str) -> str:
        if not value:
            raise ValueError("source location path cannot be the project root")
        return value


class FrontendTarget(CoditoModel):
    """Snapshot-bound target by semantic element ID or viewport coordinates."""

    snapshot_id: OpaqueId
    element_id: FrontendElementId | None = None
    x: int | None = Field(default=None, ge=0, le=2047)
    y: int | None = Field(default=None, ge=0, le=2047)

    @model_validator(mode="after")
    def exactly_one_target(self) -> FrontendTarget:
        by_element = self.element_id is not None
        by_point = self.x is not None and self.y is not None
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be provided together")
        if by_element == by_point:
            raise ValueError("target requires exactly one of element_id or x/y")
        return self


class FrontendSessionStartInput(CoditoModel):
    operation: Literal["session_start"] = "session_start"
    project_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1_000)
    route: str = Field(
        default="/",
        max_length=MAX_FRONTEND_URL_CHARS,
        json_schema_extra={"pattern": FRONTEND_ROUTE_PATTERN},
    )
    viewport: FrontendViewport = Field(default_factory=FrontendViewport)
    ready_timeout_seconds: int = Field(default=30, ge=5, le=120)
    idempotency_key: OpaqueId

    _valid_route = field_validator("route")(validate_frontend_route)


class FrontendSnapshotInput(CoditoModel):
    operation: Literal["snapshot"] = "snapshot"
    project_id: OpaqueId
    session_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1_000)
    max_elements: int = Field(default=500, ge=1, le=500)


class FrontendInspectInput(FrontendTarget):
    operation: Literal["inspect"] = "inspect"
    project_id: OpaqueId
    session_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1_000)


class FrontendActInput(CoditoModel):
    operation: Literal["act"] = "act"
    project_id: OpaqueId
    session_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1_000)
    snapshot_id: OpaqueId
    element_id: FrontendElementId
    action: FrontendAction
    text: FrontendText | None = None
    key: FrontendKey | None = None
    option: str | None = Field(default=None, max_length=1_024)
    delta_x: int | None = Field(default=None, ge=-2_048, le=2_048)
    delta_y: int | None = Field(default=None, ge=-2_048, le=2_048)
    idempotency_key: OpaqueId

    @model_validator(mode="after")
    def action_fields_match(self) -> FrontendActInput:
        supplied = {
            "text": self.text is not None,
            "key": self.key is not None,
            "option": self.option is not None,
            "scroll": self.delta_x is not None or self.delta_y is not None,
        }
        if self.action is FrontendAction.FILL:
            valid = supplied["text"] and not any(
                supplied[name] for name in ("key", "option", "scroll")
            )
        elif self.action is FrontendAction.PRESS:
            valid = supplied["key"] and not any(
                supplied[name] for name in ("text", "option", "scroll")
            )
        elif self.action is FrontendAction.SELECT:
            valid = supplied["option"] and not any(
                supplied[name] for name in ("text", "key", "scroll")
            )
        elif self.action is FrontendAction.SCROLL:
            has_nonzero_delta = (self.delta_x or 0) != 0 or (self.delta_y or 0) != 0
            valid = (
                supplied["scroll"]
                and has_nonzero_delta
                and not any(supplied[name] for name in ("text", "key", "option"))
            )
        else:
            valid = not any(supplied.values())
        if not valid:
            raise ValueError(f"fields do not match frontend action {self.action.value}")
        return self


class FrontendSourceInput(CoditoModel):
    operation: Literal["source"] = "source"
    project_id: OpaqueId
    session_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1_000)
    snapshot_id: OpaqueId
    element_id: FrontendElementId


class FrontendSessionStopInput(CoditoModel):
    operation: Literal["session_stop"] = "session_stop"
    project_id: OpaqueId
    session_id: OpaqueId
    purpose: str = Field(min_length=1, max_length=1_000)
    reason: str = Field(default="Frontend review complete", min_length=1, max_length=500)


ProjectFrontendInput = (
    FrontendSessionStartInput
    | FrontendSnapshotInput
    | FrontendInspectInput
    | FrontendActInput
    | FrontendSourceInput
    | FrontendSessionStopInput
)


class FrontendDevServerInfo(CoditoModel):
    ownership: Literal["managed", "reused"]
    health: Literal["ready"] = "ready"


class FrontendSessionStartResult(CoditoModel):
    operation: Literal["session_start"] = "session_start"
    project_id: OpaqueId
    session_id: OpaqueId
    base_url: str = Field(
        max_length=MAX_FRONTEND_URL_CHARS,
        description="Bare HTTP loopback origin selected by the Windows agent.",
        json_schema_extra={"pattern": FRONTEND_MANAGED_ORIGIN_PATTERN},
    )
    url: str = Field(
        max_length=MAX_FRONTEND_URL_CHARS,
        description="Current HTTP loopback page URL without query or fragment.",
        json_schema_extra={"pattern": FRONTEND_MANAGED_URL_PATTERN},
    )
    viewport: FrontendViewport
    dev_server: FrontendDevServerInfo
    warnings: list[str] = Field(default_factory=list, max_length=16)

    _valid_base_url = field_validator("base_url")(_validate_managed_origin)
    _valid_url = field_validator("url")(_validate_managed_url)


class FrontendElement(CoditoModel):
    element_id: FrontendElementId
    parent_id: FrontendElementId | None = None
    tag: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9-]*$")
    role: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=1_024)
    text: FrontendText | None = None
    box: FrontendRect
    visible: bool
    enabled: bool = True
    focused: bool = False
    classes: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def valid_parent(self) -> FrontendElement:
        if self.parent_id == self.element_id:
            raise ValueError("frontend element cannot be its own parent")
        if any(not item or len(item) > MAX_FRONTEND_CLASS_CHARS for item in self.classes):
            raise ValueError("frontend classes must be nonempty and bounded")
        return self


class FrontendConsoleEntry(CoditoModel):
    level: Literal["debug", "info", "warning", "error"]
    text: str = Field(min_length=1, max_length=4_096)


class FrontendNetworkIssue(CoditoModel):
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    url: str = Field(
        min_length=1,
        max_length=MAX_FRONTEND_URL_CHARS,
        description="HTTP(S) URL or relative resource path with query and fragment redacted.",
    )
    status: int | None = Field(default=None, ge=100, le=599)
    failure: str | None = Field(default=None, max_length=1_024)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.username is not None
            or parsed.password is not None
            or any(
                ord(character) < FIRST_PRINTABLE or ord(character) == DELETE_CONTROL
                for character in value
            )
        ):
            raise ValueError("network URL cannot contain credentials or control characters")
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            raise ValueError("network URL must be relative or HTTP/HTTPS")
        if parsed.query or parsed.fragment:
            raise ValueError("network URL query and fragment must be redacted")
        return value


class FrontendSnapshotResult(PngImageResult):
    operation: Literal["snapshot"] = "snapshot"
    project_id: OpaqueId
    session_id: OpaqueId
    snapshot_id: OpaqueId
    url: str = Field(
        max_length=MAX_FRONTEND_URL_CHARS,
        description="Current HTTP loopback page URL without query or fragment.",
        json_schema_extra={"pattern": FRONTEND_MANAGED_URL_PATTERN},
    )
    title: str = Field(max_length=1_024)
    viewport: FrontendViewport
    elements: list[FrontendElement] = Field(max_length=500)
    console: list[FrontendConsoleEntry] = Field(default_factory=list, max_length=100)
    network: list[FrontendNetworkIssue] = Field(default_factory=list, max_length=100)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list, max_length=16)

    _valid_url = field_validator("url")(_validate_managed_url)

    @model_validator(mode="after")
    def valid_element_registry(self) -> FrontendSnapshotResult:
        if self.width != self.viewport.width or self.height != self.viewport.height:
            raise ValueError("snapshot PNG dimensions must equal the requested viewport")
        identifiers = {item.element_id for item in self.elements}
        if len(identifiers) != len(self.elements):
            raise ValueError("snapshot element IDs must be unique")
        if any(
            item.parent_id is not None and item.parent_id not in identifiers
            for item in self.elements
        ):
            raise ValueError("snapshot parent IDs must refer to an element in the same snapshot")
        return self


class FrontendSnapshotMetadata(PngImageMetadata):
    """Model-visible snapshot data after PNG bytes move to MCP ImageContent."""

    operation: Literal["snapshot"] = "snapshot"
    project_id: OpaqueId
    session_id: OpaqueId
    snapshot_id: OpaqueId
    url: str = Field(
        max_length=MAX_FRONTEND_URL_CHARS,
        description="Current HTTP loopback page URL without query or fragment.",
        json_schema_extra={"pattern": FRONTEND_MANAGED_URL_PATTERN},
    )
    title: str = Field(max_length=1_024)
    viewport: FrontendViewport
    elements: list[FrontendElement] = Field(max_length=500)
    console: list[FrontendConsoleEntry] = Field(default_factory=list, max_length=100)
    network: list[FrontendNetworkIssue] = Field(default_factory=list, max_length=100)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list, max_length=16)

    _valid_url = field_validator("url")(_validate_managed_url)

    @model_validator(mode="after")
    def valid_element_registry(self) -> FrontendSnapshotMetadata:
        if self.width != self.viewport.width or self.height != self.viewport.height:
            raise ValueError("snapshot PNG dimensions must equal the requested viewport")
        identifiers = {item.element_id for item in self.elements}
        if len(identifiers) != len(self.elements):
            raise ValueError("snapshot element IDs must be unique")
        if any(
            item.parent_id is not None and item.parent_id not in identifiers
            for item in self.elements
        ):
            raise ValueError("snapshot parent IDs must refer to an element in the same snapshot")
        return self


class FrontendAccessibility(CoditoModel):
    role: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=1_024)
    description: str | None = Field(default=None, max_length=2_048)
    disabled: bool | None = None
    expanded: bool | None = None
    checked: bool | Literal["mixed"] | None = None


class FrontendStyleProperty(CoditoModel):
    name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^--?[A-Za-z][A-Za-z0-9_-]*$|^[A-Za-z][A-Za-z0-9_-]*$",
    )
    value: FrontendCssText


class FrontendMatchedRule(CoditoModel):
    selector: str = Field(min_length=1, max_length=2_048)
    declarations: list[FrontendStyleProperty] = Field(default_factory=list, max_length=128)
    source: None = Field(
        default=None,
        description="Reserved for future stylesheet source maps; always null in Codito 0.3.0.",
    )


class FrontendInspectResult(CoditoModel):
    operation: Literal["inspect"] = "inspect"
    project_id: OpaqueId
    session_id: OpaqueId
    snapshot_id: OpaqueId
    element: FrontendElement
    computed_styles: list[FrontendStyleProperty] = Field(default_factory=list, max_length=128)
    matched_rules: list[FrontendMatchedRule] = Field(default_factory=list, max_length=128)
    accessibility: FrontendAccessibility
    warnings: list[str] = Field(default_factory=list, max_length=16)


class FrontendActResult(CoditoModel):
    operation: Literal["act"] = "act"
    project_id: OpaqueId
    session_id: OpaqueId
    snapshot_id: OpaqueId
    element_id: FrontendElementId
    action: FrontendAction
    status: Literal["completed"] = "completed"
    snapshot_invalidated: Literal[True] = True
    warnings: list[str] = Field(default_factory=list, max_length=16)


class FrontendSourceReference(CoditoModel):
    location: FrontendSourceLocation
    name: str | None = Field(default=None, max_length=512)


class FrontendSourceResult(CoditoModel):
    operation: Literal["source"] = "source"
    project_id: OpaqueId
    session_id: OpaqueId
    snapshot_id: OpaqueId
    element_id: FrontendElementId
    confidence: Literal["exact", "heuristic", "unavailable"]
    consumer: FrontendSourceReference | None = None
    design_system: list[FrontendSourceReference] = Field(default_factory=list, max_length=32)
    styles: list[FrontendSourceReference] = Field(
        default_factory=list,
        max_length=0,
        description=(
            "Reserved for a future source-map-aware stylesheet adapter; empty in Codito 0.3.0."
        ),
    )
    warnings: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def unavailable_has_no_sources(self) -> FrontendSourceResult:
        has_source = self.consumer is not None or bool(self.design_system) or bool(self.styles)
        if self.confidence == "unavailable" and has_source:
            raise ValueError("unavailable source mapping cannot include source locations")
        if self.confidence != "unavailable" and not has_source:
            raise ValueError("exact or heuristic source mapping requires a source location")
        return self


class FrontendSessionStopResult(CoditoModel):
    operation: Literal["session_stop"] = "session_stop"
    project_id: OpaqueId
    session_id: OpaqueId
    status: Literal["stopped", "already_stopped"]
    dev_server_stopped: bool
    warnings: list[str] = Field(default_factory=list, max_length=16)


ProjectFrontendResult = (
    FrontendSessionStartResult
    | FrontendSnapshotResult
    | FrontendInspectResult
    | FrontendActResult
    | FrontendSourceResult
    | FrontendSessionStopResult
)


_INPUT_ADAPTER: TypeAdapter[ProjectFrontendInput] = TypeAdapter(ProjectFrontendInput)
_RESULT_ADAPTER: TypeAdapter[ProjectFrontendResult] = TypeAdapter(ProjectFrontendResult)


def validate_project_frontend(value: Any) -> ProjectFrontendInput:
    return _INPUT_ADAPTER.validate_python(value)


def validate_project_frontend_result(value: Any) -> ProjectFrontendResult:
    return _RESULT_ADAPTER.validate_python(value)
