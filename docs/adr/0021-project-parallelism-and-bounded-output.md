# ADR 0021: Project-parallel execution and bounded shared-tunnel output

- Status: Accepted
- Date: 2026-09-11

## Context

Codito carries requests for every project registered on one device through the same outbound
WebSocket. The agent already creates an independent asyncio task for every admitted operation and
serializes patch/shell mutation by `project_id`, not by device. However, two shared resources could
still create head-of-line blocking between otherwise independent projects:

1. Relay ORM calls used Asgiref's process-wide thread-sensitive fallback. Independent MCP requests
   therefore queued behind one synchronous database lane inside each ASGI worker.
2. A shell status response could contain the job's entire unread output, up to 10 MiB. Serializing
   and sending that response under the device socket's required send lock delayed small results
   for other projects.

Starting the .NET broker also had a smaller cold-start race: simultaneous first commands could each
launch an identical capability probe before the cached result existed.

GPU execution does not address these bottlenecks. Relay dispatch is database/network I/O, and shell
supervision is event-loop coordination. A GPU remains available to an explicitly invoked child
tool, but is not part of the routing critical path.

## Decision

1. Run each complete synchronous Django ORM unit in a process-wide bounded executor. The default is
   eight database lanes per relay worker and is configurable with `DATABASE_ASYNC_WORKERS` from 2
   through 32.
2. Never pass a Django connection, cursor, transaction, or lazy queryset across the async boundary.
   Each submitted function owns its full ORM/transaction lifetime. Clean old/unusable thread-local
   connections before and after every unit while retaining healthy persistent connections.
3. Enforce the global per-device pending limit inside a transaction protected by a short
   `select_for_update` lock on that device. Only admission is serialized; accepted operations do
   not retain the lock while waiting for Redis, the agent, approval, or execution.
4. Reuse one bounded asynchronous Redis connection pool per relay worker instead of creating and
   closing a pool for every MCP operation. The pool defaults to 64 connections, supports concurrent
   blocking Stream reads, waits up to five seconds for a pool lane rather than failing immediately
   during a burst, and is closed by the ASGI lifespan.
5. Keep network waits, Redis Stream reads, WebSocket I/O, and MCP request handling on asyncio. The
   database pool must never contain Redis or device-response waits.
6. Preserve the agent's SQLite persist-before-ack ordering, but run durable receipt/state/idempotency
   access in worker threads. The coroutine still awaits every required commit before sending the
   corresponding acknowledgement or terminal result, so durability is unchanged without blocking
   heartbeat, output capture, or another project's coroutine during filesystem synchronization.
7. Partition bounded read capacity by project before acquiring the device-wide limit. The agent
   permits eight concurrent reads by default, with at most four from one project. A backlog from
   one project therefore cannot occupy every read slot, while a single project retains the same
   four-read capacity it had before this decision.
8. Keep mutation exclusion scoped to one project. Separate projects may execute shell jobs and file
   work concurrently; conflicting patch/shell mutation inside one project remains explicit rather
   than becoming an unsafe global parallel write.
9. Page shell output by UTF-8 byte budget. `shell_status` defaults to 256 KiB and allows 16 KiB to
   1 MiB per response. `next_sequence_cursor`, `available_sequence_cursor`, and `has_more_output`
   make continuation lossless for independent consumers.
10. Slice shell output directly from its contiguous sequence cursor so status cost is proportional to
   newly returned chunks rather than all historical chunks.
11. Make the first broker capability probe single-flight. Concurrent command starts share its result;
   later calls use the established cache.

## Consequences

- Independent MCP requests and project operations no longer share one implicit ORM queue.
- One noisy build cannot monopolize the shared WebSocket with a multi-megabyte status envelope.
- Relay Redis connections are reused, bounded, and observable instead of being churned per request.
- Database concurrency and connection count remain bounded: deployed capacity is ASGI workers
  multiplied by `DATABASE_ASYNC_WORKERS`.
- Clients must continue polling a terminal shell job while `has_more_output` is true.
- This is hybrid delivery: event-driven WebSocket/Redis internally and bounded long-poll at the MCP
  tool boundary. It does not invent a proprietary streaming protocol; the MCP Tasks adapter from
  ADR 0017 remains the future host-negotiated continuation layer.

## Verification

- An isolated eight-call scheduler benchmark with 100 ms units dropped from 0.807 seconds on the
  global thread-sensitive lane to 0.107 seconds on the bounded pool. This measures removed relay
  serialization, not production database throughput.
- Regression tests cover parallel project shell starts, per-project read fairness, broker probe
  single-flight, shell output continuation, shared Redis reuse/shutdown, overlapping database lanes,
  and PostgreSQL device-admission locking.
