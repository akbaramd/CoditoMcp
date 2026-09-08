# ADR 0001: Python modular monolith and Windows companion

- Status: Accepted
- Date: 2026-09-08

## Context

Codito needs a web identity plane, an MCP endpoint, live device routing, a Windows
tray application, and security-sensitive Windows primitives. A distributed relay
would add failure modes and operational cost before traffic requires it. Python can
share validation and domain logic across both applications, but it should not
reimplement AppContainer, Job Object, CNG, or handle-safe Win32 code.

## Decision

- Use Python 3.12, provisioned and locked with `uv`.
- Build the relay as a Django 5.2 LTS modular monolith under a Starlette ASGI root.
- Mount the official MCP Python SDK's Streamable HTTP app beside Django and the
  device WebSocket gateway.
- Build the Windows agent as an asyncio daemon plus a PySide6 per-user UI.
- Put the small Windows boundary in a self-contained .NET 8 broker with a narrow,
  versioned, authenticated local IPC contract.
- Keep shared wire contracts in `codito_protocol`; do not share persistence models.

## Consequences

One relay artifact and database simplify transactions, upgrades, and recovery. Two
initial ASGI workers are safe because the MCP transport does not require server-side
MCP sessions; live routing uses Redis and durable operation state uses PostgreSQL.
Python/.NET packaging is more involved, but native boundary code remains auditable
and testable with platform APIs rather than fragile FFI spread through the daemon.
