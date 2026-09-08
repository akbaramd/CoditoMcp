# ADR 0014 — Consented primary-display screenshots

Accepted 2026-09-08 following the owner's request for ChatGPT to inspect the running
Windows screen. Adds the sixth tool, `device_screenshot`.

Input: purpose, `display=primary`, max_dimension 640–2048 (default 1600).
OAuth scope: **screen:read**; existing grants are not silently expanded. The
connection must authorize this scope before using the new tool.

Each capture requires a separate one-shot Windows approval. File, native shell,
read-always and shell-always grants do not authorize screen capture. The warning
explicitly includes visible private windows and transmission to ChatGPT/relay.

The daemon queues a capture **only after** approval and verifies security generation
and deadline before and after. The interactive Qt companion captures the primary
screen, refuses non-default/locked/secure input desktops, scales and encodes PNG.
No elevated service, input injection, webcam, hidden recorder, video or keyboard/mouse
control is provided. Multiple monitors/all-window capture and persistent screen grants
are deferred. Protected/DRM surfaces may be blank; this is not a bypass of Windows.

Output is at most 600 KB PNG / 800 KB base64, with width/height, capture time and
SHA-256. The relay validates the shared result and returns MCP ImageContent alongside
metadata and concise text. Pixels are not duplicated in structured text.

SQLite/PostgreSQL durable result journals store a non-replayable receipt/digest,
not image bytes. A crash recovery asks for a new capture instead of replaying pixels.
Transport memory and Redis result streams may contain the image for the existing
five-minute TTL; Redis persistence/backups and the receiving ChatGPT account have
their own retention. This is not a claim of zero disk retention everywhere.

Tests use a generated 1x1 PNG or mocked Qt screen, never secretly capture the user's
desktop to satisfy a unit test. Actual on-device consent and a real ChatGPT image
render are manual acceptance checks.
