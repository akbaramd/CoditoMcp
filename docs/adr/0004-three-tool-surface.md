# ADR 0004: Exactly three MCP tools

- Status: Accepted
- Date: 2026-09-08

## Context

Models work more reliably with a small, stable tool surface. Separate tools for
every filesystem operation increase authorization and compatibility complexity;
an unrestricted filesystem or terminal tool is unsafe.

## Decision

Expose exactly:

- `project_read`, discriminated by `operation` into project listing, directory
  listing, bounded file reads, and bounded text search.
- `project_apply_patch`, accepting only the anchored `*** Begin Patch` grammar,
  explicit base hashes, an idempotency key, and optional dry run.
- `project_shell`, discriminated by `action=start|poll|cancel`, without a PTY.

Unknown input fields are forbidden. Project IDs and paths are opaque/project-
relative. The model cannot supply `approved`, `trusted`, local roots, an execution
mode, or a device override. Results combine structured JSON with short readable
text, stable IDs, typed errors, and accurate MCP annotations/security schemes.

## Consequences

New operations normally become variants of one of the three tools and require
schema/version review. A fourth tool requires a superseding ADR and compatibility
plan. Shell starts are never replayed after an uncertain acknowledgment.
