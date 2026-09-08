# Critical sequences

## Invite-only account creation

```mermaid
sequenceDiagram
  actor Admin
  participant Relay
  actor User
  Admin->>Relay: Create email-bound invite (24h)
  Relay-->>Admin: Single-use URL (shown once)
  Admin->>User: Deliver manually out of band
  User->>Relay: Open URL and submit email/password
  Relay->>Relay: Constant-time hash lookup; validate email, expiry, unused
  Relay->>Relay: Argon2id password + mark invite consumed atomically
  Relay-->>User: New rotated browser session
  Note over User,Relay: URL possession is not independent email verification
```

## Desktop login and enrollment

```mermaid
sequenceDiagram
  participant Agent
  participant Browser
  participant Relay
  participant CNG as Windows CNG/TPM
  Agent->>Agent: Generate PKCE verifier/challenge and loopback state
  Agent->>Browser: Open system browser authorization URL
  Browser->>Relay: Login + authorize desktop public client
  Relay-->>Agent: Loopback code + state
  Agent->>Relay: Code + verifier + exact redirect URI
  Relay-->>Agent: Short access/rotating refresh tokens
  Agent->>CNG: Create non-exportable signing key
  Agent->>Relay: Enroll public key with authenticated account
  Relay-->>Agent: Device ID and one-time challenge
  Agent->>Relay: Signed challenge
  Relay-->>Agent: Device active; create/rotate MCP link
```

## MCP operation and durable acknowledgment

```mermaid
sequenceDiagram
  participant GPT as ChatGPT
  participant MCP as Relay MCP worker
  participant DB as PostgreSQL
  participant RS as Redis Streams
  participant WS as Relay socket worker
  participant Agent
  participant LDB as Agent SQLite
  GPT->>MCP: project_read/apply_patch/shell + OAuth
  MCP->>MCP: Validate resource, scope and all ownership bindings
  MCP->>DB: INSERT operation=accepted, digest, deadline
  DB-->>MCP: operation ID
  MCP->>RS: Publish fenced dispatch reference
  RS-->>WS: Consumer-group delivery
  WS->>Agent: operation envelope(epoch=N)
  Agent->>LDB: INSERT received + payload digest (commit)
  Agent-->>WS: operation_received(epoch=N)
  WS->>DB: Mark received
  Agent-->>WS: operation_started / progress
  Agent->>LDB: Persist terminal result
  Agent-->>WS: operation_result
  WS->>DB: Store terminal state/result metadata
  WS-->>Agent: terminal_ack
  MCP-->>GPT: Structured result + concise text
```

## Reconnect and fencing

```mermaid
sequenceDiagram
  participant A as Agent
  participant W1 as Worker/socket epoch 41
  participant DB
  participant W2 as Worker/new socket
  A-xW1: Network interruption
  A->>A: Full-jitter backoff; refresh ticket if needed
  A->>W2: WSS + ticket + key proof
  W2->>DB: Atomic epoch 41 -> 42 and claim connection
  DB-->>W2: epoch 42
  W2-->>A: welcome(epoch=42)
  W1->>DB: Attempt result at epoch 41
  DB-->>W1: fenced; reject and close
  A->>W2: Resume journal/output state for epoch 42
```

## Approval

```mermaid
sequenceDiagram
  participant D as Daemon
  participant UI
  actor User
  D->>D: Derive canonical action digest and risk facts
  D->>UI: Signed named-pipe approval request
  UI-->>User: Toast opens persistent detail dialog
  Note over UI,User: Toast action itself cannot approve
  User->>UI: Deny / Allow once / eligible session grant
  UI->>D: Signed decision bound to request, epoch, expiry
  D->>D: Recompute digest and re-evaluate eligibility
  alt valid approval
    D->>D: Execute exact action once
  else mismatch, expiry, lock, disconnect, or replay
    D->>D: Deny and audit reason
  end
```

## Shell approval continuation

```mermaid
sequenceDiagram
  participant GPT as MCP client / ChatGPT
  participant Relay
  participant Agent
  participant UI as Windows approval UI
  actor User
  GPT->>Relay: project_shell(start)
  Relay->>Agent: operation over outbound WSS
  Agent->>Agent: Create durable job_id; state=pending_approval
  Agent->>UI: Request local approval
  par bounded start wait
    Agent->>Agent: Wait on state-change condition (<=30s)
  and user decision
    UI-->>User: Show approval
    User->>UI: Allow / deny
  end
  alt decision arrives inside bounded wait
    Agent-->>Relay: Same job_id, queued/running/terminal
    Relay-->>GPT: Start result
  else decision takes longer
    Agent-->>Relay: Same job_id, pending_approval
    Relay-->>GPT: Start result
    loop while job is non-terminal
      GPT->>Relay: project_shell(poll, same job_id, wait=30s)
      Relay->>Agent: Poll over WSS
      Agent-->>Relay: Current state + sequenced output
      Relay-->>GPT: Poll result
    end
  end
  Note over GPT,Agent: Never resubmit start to continue the same intended action.
```

When a client explicitly advertises the official `io.modelcontextprotocol/tasks` extension,
Codito can later expose the same durable state through a Tasks adapter. The shell-job path above
remains the compatibility path for clients that do not negotiate Tasks.
