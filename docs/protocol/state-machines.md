# State machines

## Device connection

```mermaid
stateDiagram-v2
  [*] --> Offline
  Offline --> Authenticating: network available + ticket/proof
  Authenticating --> Online: welcome(new epoch)
  Authenticating --> Backoff: rejected/timeout
  Online --> Suspect: heartbeat overdue
  Suspect --> Online: valid heartbeat
  Suspect --> Backoff: 75s offline threshold
  Online --> Backoff: socket lost
  Backoff --> Authenticating: full-jitter timer
  Online --> Revoked: device/link/grant revoked
  Revoked --> [*]
```

## Operation

```mermaid
stateDiagram-v2
  [*] --> Validating
  Validating --> Rejected: auth/policy/offline/queue/deadline
  Validating --> Accepted: PostgreSQL commit
  Accepted --> Dispatched: Redis + current epoch socket
  Dispatched --> Received: agent SQLite commit + ack
  Received --> PendingApproval: local policy requires user
  PendingApproval --> Denied: user/expiry/lock/disconnect
  PendingApproval --> Started: valid one-shot approval
  Received --> Started: isolated or locally trusted policy
  Started --> Progress
  Progress --> Progress
  Started --> Succeeded
  Progress --> Succeeded
  Started --> Failed
  Progress --> Failed
  Started --> Cancelled
  Dispatched --> Expired: no receipt before deadline
  Received --> OutcomeUnknown: shell start ack boundary uncertain
  Succeeded --> Acknowledged: relay terminal_ack
  Failed --> Acknowledged
  Cancelled --> Acknowledged
  Denied --> Acknowledged
```

Terminal state is immutable. `outcome_unknown` never transitions to retry; a later
reconciliation may attach evidence but cannot pretend the first command did not run.

## Patch transaction

```mermaid
stateDiagram-v2
  [*] --> Parsed
  Parsed --> Conflict: syntax/path/precondition error
  Parsed --> Preflighted: all hunks exact and policy passes
  Preflighted --> DryRunComplete: dry_run
  Preflighted --> JournalPrepared: journal fsync
  JournalPrepared --> Staged: sibling files fsync
  Staged --> Committing
  Committing --> Committed: manifest + completion marker fsync
  Committing --> RecoveryRequired: crash/error
  RecoveryRequired --> Committed: new manifest verified
  RecoveryRequired --> RolledBack: old manifest restored
  RecoveryRequired --> Disabled: neither manifest provable
```

## Approval grant

```mermaid
stateDiagram-v2
  [*] --> Proposed
  Proposed --> Denied: explicit deny
  Proposed --> OneShot: allow once + signed exact digest
  Proposed --> Session: eligible capability only
  OneShot --> Consumed: first exact action
  Session --> Session: matching action + touch idle timer
  Session --> Expired: 30m absolute or 10m idle
  Session --> Cleared: lock/sleep/logout/restart/revocation/disconnect >60s
```
