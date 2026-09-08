# ADR 0006: Absolute paths remain on the device

- Status: Accepted
- Date: 2026-09-08

## Context

Local paths reveal usernames, organization names, topology, and sensitive project
locations. The relay only needs enough metadata to route an authorized operation.

## Decision

- The relay stores opaque project ID, user-chosen title, root identity fingerprint,
  owning account/device, mode summary, status, and sanitized audit metadata.
- The Windows agent alone maps the opaque project ID to an absolute directory.
- Tool requests and results use normalized forward-slash project-relative paths.
- The agent accepts only fixed local NTFS/ReFS roots. It rejects drive roots, UNC,
  removable media, cloud placeholders, and reparse-point roots in v1.
- File resolution and mutation validate every component using Win32 handles and
  compare root volume/file identities after reopen.

## Consequences

Relay support cannot diagnose a user's local path from server data. Project
re-enrollment is required when root identity changes. Audit logs use relative paths
only when policy allows and otherwise record an action digest/counts.
