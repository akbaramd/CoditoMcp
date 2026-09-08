# ADR 0017: Hybrid durable async operations and approval continuation

- Status: Accepted
- Date: 2026-09-08

## Context

Codito already uses the right transport for the device boundary: the Windows agent owns an
outbound authenticated WebSocket, while ChatGPT reaches the relay through OAuth-protected MCP
Streamable HTTP. PostgreSQL is authoritative operation state, Redis Streams is live routing, and
the agent journals locally in SQLite.

The observed approval UX defect is therefore not a missing WebSocket. `project_shell start`
intentionally created a durable local job and returned `pending_approval` immediately. A model
then had to issue a later poll. If the assistant ended its turn to tell the user to approve, no
server push could spontaneously resume that completed chat turn.

The MCP 2026-07-28 Tasks extension explicitly targets long-running and human-in-the-loop work.
Codito's installed MCP Python SDK 2.2.0 implements the generic extension framework and per-request
capability negotiation, but it does not bundle a task-specific runtime/model package. More
importantly, Tasks are opt-in per request: Codito MUST NOT return a task handle unless the calling
host advertises `io.modelcontextprotocol/tasks`. Current hosts may still connect without that
extension, so task support cannot replace the ordinary tool-result path.

References:

- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://tasks.extensions.modelcontextprotocol.io/
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports

## Decision

1. Keep MCP Streamable HTTP stateless at the ChatGPT/relay boundary. Commands and queries remain
   ordinary request/response operations and horizontally scalable relay workers keep no hidden
   business session state.
2. Keep the outbound device WebSocket for live relay/agent delivery, heartbeat, fencing and
   lifecycle events. Do not add a second browser/client WebSocket solely for approvals.
3. Keep PostgreSQL as durable truth, Redis Streams as live cross-worker routing, and SQLite as the
   agent crash/replay journal. Socket presence never becomes the source of truth.
4. Treat shell execution as a durable asynchronous job. A start request returns a stable `job_id`;
   execution/output continues independently and poll/cancel operate on that same job.
5. Add a bounded **interaction wait** to shell start. By default the agent waits up to 30 seconds
   for the first lifecycle transition (`pending_approval -> queued/running/terminal`) using an
   `asyncio.Condition`, not periodic polling. Approval completion wakes the waiting call before
   native process creation, leaving response-budget headroom.
6. If the bounded wait expires, return `pending_approval` without cancelling the durable job.
   The caller must long-poll the same job with `wait_milliseconds` (maximum 30 seconds) and must
   never submit a second start for the same intended action.
7. Tool guidance tells agentic clients to continue long-polling in the same assistant turn instead
   of asking the user to send another chat message merely to resume the operation.
8. Add `io.modelcontextprotocol/tasks` as an optional adapter when host support is confirmed. The
   adapter maps Codito's durable `Operation`/shell-job state to `tasks/get`, `tasks/update`, and
   `tasks/cancel`, and is returned only when the current request advertises that extension. The
   ordinary synchronous/fallback result path remains mandatory for non-supporting clients.
9. Do not invent a Codito-specific task protocol. The internal durable state model stays
   transport-independent so the official Tasks extension remains an adapter rather than a rewrite.

## Consequences

- Fast approvals normally complete the approval handoff inside one tool call and one assistant
  turn.
- Slow approvals still degrade safely to bounded long-polling with the same job identity.
- No long-lived ChatGPT WebSocket, sticky MCP session, or in-memory relay workflow is required.
- Disconnects do not erase accepted work; recovery remains governed by PostgreSQL/SQLite state,
  idempotency keys, operation deadlines and connection epochs.
- The 30-second wait is intentionally shorter than the relay operation budget. It reduces latency
  without making shell process duration part of the initial MCP request.
- MCP Tasks can be layered on for supporting hosts without changing the Windows security boundary,
  relay/agent tunnel, or fallback behavior for hosts that do not negotiate the extension.
