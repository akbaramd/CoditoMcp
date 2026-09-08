# System architecture

## Context

Codito lets an authorized ChatGPT session invoke bounded operations against a
project registered on a particular user's Windows device. The relay is a control
and routing plane; the device is the filesystem and execution authority.

```mermaid
flowchart LR
  subgraph Internet[Internet trust boundary]
    GPT[ChatGPT / MCP client]
    Browser[User browser]
  end

  subgraph Edge[User-managed edge]
    Proxy[Nginx / OPNsense\nTLS termination]
  end

  subgraph Relay[Codito relay - 192.168.200.39:8094]
    ASGI[Starlette ASGI root]
    Django[Django accounts, OAuth, UI]
    MCP[MCP Streamable HTTP]
    WSG[Device WebSocket gateway]
    PG[(PostgreSQL\ndurable truth)]
    Redis[(Redis Streams\nlive dispatch)]
    ASGI --> Django
    ASGI --> MCP
    ASGI --> WSG
    Django --> PG
    MCP --> PG
    MCP --> Redis
    WSG --> PG
    WSG --> Redis
  end

  subgraph Device[Windows 11 per-user boundary]
    Daemon[Python daemon\nWSS + journals]
    UI[PySide6 tray/UI\napprovals]
    Pipe[Authenticated named pipe]
    DB[(SQLite journal +\nlocal project map)]
    Broker[.NET 8 broker\nWin32 security APIs]
    Root[(Registered project root)]
    Sandbox[AppContainer + Job Object]
    UI <--> Pipe <--> Daemon
    Daemon --> DB
    Daemon <--> Broker
    Broker --> Root
    Broker --> Sandbox
  end

  GPT -->|HTTPS OAuth + MCP| Proxy --> ASGI
  Browser -->|HTTPS UI / consent| Proxy
  Daemon -->|outbound WSS only| Proxy
```

## Relay modules

| Module | Responsibility | Durable state |
|---|---|---|
| Accounts | Invite, password, session, admin bootstrap/reset | PostgreSQL |
| OAuth | Discovery, authorization, consent, tokens, CIMD/JWKS | PostgreSQL + signing key configuration |
| Registry | Devices, links, projects, revocation, status | PostgreSQL |
| MCP | Three tool definitions, auth context, input/result translation | Operations in PostgreSQL |
| Gateway | Proof-bound device sockets, connection epochs, envelope checks | PostgreSQL + Redis |
| Dispatch | Admission, deadlines, queues, concurrency, notification | PostgreSQL + Redis Streams |
| Audit | Redacted security/event history and retention | PostgreSQL |
| Dashboard | Server-rendered HTMX account/device/project/audit views | No separate state |

The top-level Starlette router mounts modules without making Django state optional.
The MCP application is sessionless from Codito's perspective; business operation
state is explicit in PostgreSQL rather than hidden in an ASGI worker.

## Device modules

| Module | Responsibility |
|---|---|
| Connection manager | Ticket refresh, WSS, heartbeat, jitter reconnect, epoch state |
| Operation journal | Persist-before-ack, deduplication, crash recovery, terminal acks |
| Project registry | Local root map, identity fingerprints, execution mode |
| Path resolver | Lexical rejection plus handle-based containment/reparse verification |
| Read engine | Bounded directory/read/search operations and continuation cursors |
| Patch engine | Exact parser, all-file preflight, journaled atomic commit/recovery |
| Shell supervisor | Start/poll/cancel, output sequencing/caps, reconnect grace |
| Policy engine | Local modes, approval eligibility, expiring session grants |
| UI bridge | Mutually authenticated named-pipe messages and signed approval dialogs |
| Broker client | Narrow request protocol to .NET boundary; fail-closed self-test |

## Trust invariants

1. A link ID is public routing data. It never grants access.
2. An OAuth token is insufficient unless its account, active grant, resource,
   scopes, link, device, and project all agree with server state.
3. The relay cannot select or reconstruct a local absolute path.
4. The model cannot select local trust or approve its own request.
5. A stale socket cannot complete a new-epoch operation.
6. The relay records an operation before publishing it; the device records it before
   acknowledging it.
7. An isolated command never silently becomes native.

## Scale and availability

The MVP runs two ASGI workers, one PostgreSQL instance, and one Redis instance.
Workers are stateless with respect to socket routing beyond their live connection;
epoch ownership and Redis notifications coordinate them. This removes sticky MCP
sessions but does not make the single host highly available. PostgreSQL or Redis
outages make readiness fail and admission stops safely.
