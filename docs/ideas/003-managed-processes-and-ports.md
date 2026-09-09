# Managed Processes and Ports

## Status

Partially realized by ADR 0020 for frontend dev-server ownership. General-purpose
process and port tools remain an enhancement idea.

## Problem

Codito can execute bounded shell jobs, but coding workflows frequently require long-running processes such as APIs, frontend dev servers, workers, watchers, and local infrastructure. Treating these as arbitrary shell commands makes lifecycle, logs, readiness, cleanup, and ownership harder to reason about.

Developers also repeatedly need to know which ports are listening, which process owns a port, and when a newly started service becomes ready.

## Idea

Introduce Codito-managed development processes with explicit lifecycle and structured port visibility.

Potential tools:

- `process_start` — start a Codito-owned development process and return a stable process identifier.
- `process_status` — return state, PID, start time, exit information, and known runtime metadata.
- `process_logs` — read bounded sequenced stdout/stderr for the managed process.
- `process_stop` — terminate the owned process tree safely.
- `port_list` — list listening ports and associated process metadata within the allowed device policy.
- `port_wait` — wait for a requested port/readiness condition with a bounded timeout.

## Why This Matters

It enables reliable workflows such as:

```text
start API
  -> wait for port 5000
  -> run HTTP/browser tests
  -> inspect logs if something fails
  -> stop the API
```

The key goal is not simply another way to start a process. Codito should own and track the lifecycle so ChatGPT can safely orchestrate development services without relying on unmanaged detached shell processes.

## Design Considerations

- Processes should remain bounded by local Codito policy and explicit project authority.
- A managed process needs a stable opaque ID independent of the Windows PID.
- Child-process trees need deterministic cleanup.
- Logs need bounded storage, cursors, truncation semantics, and honest loss reporting.
- Port inspection should not silently broaden filesystem or process authority.
- Device disconnect/restart behavior needs explicit semantics.

## Next Step

Design the managed-process state machine and ownership/security model before exposing process and port tools publicly.
