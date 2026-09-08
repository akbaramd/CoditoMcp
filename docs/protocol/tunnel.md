# Device tunnel protocol v1

The Windows agent initiates one authenticated WebSocket to
`wss://codito.akbaramd.ir/ws/device`. TLS terminates at the user-managed edge and is
proxied to the relay. The socket is never accepted on bearer possession alone: a
short-lived single-use ticket is bound to a nonce proof by the enrolled device key.

## Envelope

All application messages use `TunnelEnvelope` JSON:

```json
{
  "protocol_version": "1.0",
  "kind": "operation",
  "message_id": "message_01J123456789ABCDEF",
  "correlation_id": "operation_01J123456789ABCDEF",
  "sequence": 1,
  "connection_epoch": 42,
  "sent_at": "2026-09-08T10:15:30Z",
  "deadline_at": "2026-09-08T10:20:30Z",
  "bindings": {
    "account_id": "account_01J123456789ABCDEF",
    "device_id": "device_01J123456789ABCDEF",
    "link_id": "link_01J123456789ABCDEF",
    "grant_id": "grant_01J123456789ABCDEF",
    "project_id": "project_01J123456789ABCDEF"
  },
  "action_digest": "<sha256 of the canonical complete payload>",
  "payload": {
    "tool_name": "project_read",
    "input": {"operation": "list_projects", "limit": 50}
  }
}
```

`message_id` is unique, `correlation_id` follows one operation, sequence is monotonic
inside the relevant stream, timestamps are UTC, and action digests use canonical
JSON with the `codito-action-digest-v1` domain separator. Receivers reject unknown
versions/kinds, extra fields, oversized frames, expired deadlines, mismatched
bindings, non-monotonic sequences, stale epochs, and digest mismatch.

An `operation` payload is exactly `{"tool_name": <one of the three public tools>,
"input": <that tool's validated input>}`. Its action digest covers this complete
object using canonical JSON and the v1 domain separator. Every lifecycle response
uses a new `message_id`, echoes the request `correlation_id` (which is distinct from
the message ID), and echoes the action digest. This makes progress/result/approval
records unambiguous across retries and reconnects.

## Message kinds

| Kind | Direction | Meaning |
|---|---|---|
| `hello` | Agent → relay | Device/version/capability claim after WS auth |
| `welcome` | Relay → agent | Accepted immutable epoch and timing limits |
| `heartbeat` / `heartbeat_ack` | Both | Liveness, clocks, queue/output summaries |
| `operation` | Relay → agent | Fenced persisted tool action |
| `operation_received` | Agent → relay | Agent SQLite commit completed |
| `operation_started` | Agent → relay | Irreversible execution boundary crossed |
| `operation_progress` | Agent → relay | Sequenced bounded state/output notification |
| `operation_result` | Agent → relay | Durable terminal result |
| `operation_cancel` | Relay → agent | Best-effort cancellation request |
| `terminal_ack` | Relay → agent | Terminal result durably recorded, then acknowledged |
| `error` | Both | Protocol-level typed failure, never secrets |
| `goodbye` | Both | Graceful close/reason before socket close |

## Timing and reconnection

- WebSocket ping interval and timeout: 20 seconds each.
- Application heartbeat: 30 seconds.
- Relay marks offline after 75 seconds without valid liveness.
- Reconnect uses full jitter: random delay from zero to `min(60s, 1s * 2^attempt)`.
- Refresh credentials before expiry; do not repeatedly reconnect with rejected
  credentials.
- A disconnect longer than 60 seconds clears session grants. Running shell output
  remains resumable for at most that grace, then the Job Object is terminated.

Each successful reconnect atomically increments the epoch. The new `welcome` makes
all previous sockets and queued epoch-specific envelopes stale. A worker must
compare the epoch in the same database transition that records an acknowledgment
or result.

## Backpressure

- Maximum admitted pending operations per device: 32.
- Maximum concurrent reads per device: 4.
- Patches and shell starts are serialized per project; polling/cancel remains
  responsive.
- WebSocket frame and decompressed JSON sizes are bounded. Oversized output is
  truncated/terminated according to the operation contract, never buffered without
  limit.
- No mutation is queued when the device is offline. Reads may be admitted/retried
  only within deadline and idempotency policy.
