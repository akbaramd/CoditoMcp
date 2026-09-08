# ADR 0005: PostgreSQL truth, Redis live routing, and epoch fencing

- Status: Accepted
- Date: 2026-09-08

## Context

Multiple relay workers must route to one current device socket without sticky MCP
sessions. Network interruption at any dispatch boundary creates duplicate or
unknown-outcome risks.

## Decision

- PostgreSQL is authoritative for accounts, grants, links, devices, projects,
  operations, deadlines, deduplication, and terminal state.
- Redis Streams carries fenced live dispatch and result notifications; it is not
  the only copy of an accepted operation.
- Every authenticated reconnect atomically increments a device connection epoch.
  Messages and socket ownership carry that epoch; older epochs cannot acknowledge,
  mutate, or complete work.
- Persist on the relay before dispatch. Persist in agent SQLite before sending
  `operation_received`. Terminal results require an acknowledgment.
- Reads may be retried by policy. Patches deduplicate on account/grant/device/
  project/idempotency key and hashes. Shell starts are never automatically replayed.
- Reject mutations while offline; cap pending work at 32; allow four reads per
  device and serialize patch/shell mutation per project.

## Consequences

State machines need explicit `offline`, `expired`, `cancelled`, and
`outcome_unknown` states. Redis loss pauses routing but does not erase accepted
operations. Relay and agent recovery tests must exercise every persist/dispatch/
ack/start/result boundary.
