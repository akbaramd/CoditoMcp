# ADR 0022: Physical-workspace mutation isolation and durable shell polling

- Status: Accepted
- Date: 2026-09-12
- Supersedes: ADR 0021 decision 8 (project-ID-scoped mutation exclusion)

## Context

Relay project IDs are authorization and routing identities, not filesystem lock identities. An
explicitly approved external file target or external shell working directory can make two different
project IDs address the same physical workspace. Serializing by project ID therefore permits
overlapping mutations of one root while unnecessarily coupling no truly independent roots.

Shell output is captured incrementally, but MCP tool results are bounded request/response messages.
The in-memory shell job map made terminal polling fast, yet a daemon restart discarded that lookup
even though the terminal result already existed in SQLite. Retaining every completed job in memory
also made memory use grow for the lifetime of the daemon.

Windows atomic replacement can briefly fail while an editor, build host, antivirus scanner, or
indexer holds a sharing lock. The rollback path previously rewrote the backup even when the failed
replace had left the original file unchanged. That second replace could turn a transient sharing
violation into a fail-closed recovery journal.

## Decision

1. Key patch/shell mutation exclusion by the stable fingerprint of the physical target root, never
   by relay project ID. Distinct roots remain parallel. Project aliases and explicitly authorized
   external targets that resolve to the same root are serialized.
2. A shell using an in-project working directory owns the registered project-root fingerprint. A
   shell using an external working directory owns that directory's validated fingerprint.
3. Keep shell capture event-driven and incremental inside the agent, and keep bounded long-poll at
   the MCP boundary. MCP consumers receive cursor-based pages; no proprietary byte-stream transport
   is introduced.
4. Index durable shell journal rows by `job_id`. Polling a terminal job after restart or in-memory
   eviction reads the same authorization-bound result from SQLite and reapplies the requested output
   page budget. A restart-interrupted non-terminal job reports `outcome_unknown`, never a false
   `shell_not_found`.
5. Retain at most 32 terminal shell jobs in memory. SQLite remains the terminal source of truth.
6. Retry only transient Windows atomic-replace sharing/access failures with a bounded 370 ms total
   backoff. Do not retry semantic conflicts or path-identity failures.
7. Rollback compares current, original, and intended hashes. If the original is already present, it
   performs no write. If the file changed independently, it preserves that file and remains
   fail-closed rather than overwriting newer work.
8. Recursive read/search traversal prunes standard VCS, dependency, IDE, and generated-build
   directories such as `.git`, `node_modules`, `.venv`, `.vs`, `bin`, `obj`, and `target` before
   candidate accounting. A caller can still target an ignored directory explicitly as the
   operation root.
9. Text-search continuation records the next candidate and line rather than only the number of
   matches already returned. Filling a per-call scan budget without finding a match therefore
   advances on the next page instead of repeating the same scan. The per-page byte budget is 32
   MiB after generated-directory pruning.

## Consequences

- Different projects continue to build, read, and mutate concurrently when their physical roots are
  distinct.
- Cross-project or external-root collisions become a short retryable `rate_limited` response instead
  of overlapping filesystem writes.
- Terminal shell polling survives daemon restarts and completed-job memory is bounded.
- Long-poll remains the correct interoperability boundary: streaming is used where it helps
  internally, while MCP responses remain durable, pageable, and replayable.
- Dependency trees no longer dominate ordinary whole-project searches, reducing both latency and
  `output_limit_exceeded` failures without raising safety ceilings.
- A broad no-match search can continue through a large source tree without looping on an unchanged
  cursor; each page still has a deterministic resource ceiling.

## Verification

- Regression tests prove distinct projects still start shell jobs in parallel.
- A shared external patch root is rejected even when two callers reuse the same idempotency-key text.
- A terminal shell job is polled from a fresh manager through the migrated `job_id` index.
- A failed replace that leaves the original intact performs no rollback rewrite and leaves no stale
  recovery journal.
- Recursive listing and search stay below candidate limits when a large dependency cache is present.
- Search-continuation tests cover scan-budget exhaustion with zero matches and resumption within a
  matching file.
