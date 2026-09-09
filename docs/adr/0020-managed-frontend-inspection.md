# ADR 0020 — Managed local frontend inspection

## Status

Accepted for Codito 0.3.0. This decision supersedes only ADR 0015's statement that
Codito has no managed browser profile, DOM access, or click/type/scroll capability.
ADR 0015 remains authoritative for the separate `browser_open`, selected-display,
and local-consent features.

## Context

Opening a URL in the user's browser and capturing a Windows display cannot provide
the DOM, computed CSS, accessibility tree, stable element targeting, or an exact
viewport image. Exposing a project dev server to the Internet would also turn a
local implementation detail into a new network boundary.

Frontend work needs a closed local loop:

```text
MCP client -> relay -> Windows agent
                         |-> owned/reused loopback dev server
                         `-> Codito-owned Chromium -> pixels + DOM/CDP evidence
```

## Decision

Codito adds six focused public tools backed by one hidden `project_frontend` wire
operation:

- `frontend_session_start`
- `frontend_snapshot`
- `frontend_inspect`
- `frontend_act`
- `frontend_source`
- `frontend_session_stop`

The existing `browser_open` and `screenshot_capture` tools remain independent.
They cannot create, authorize, or inspect a frontend session.

### Local runtime and configuration

The Windows agent owns the browser and, when necessary, the dev-server process
tree. It first reads `.codito/frontend.json`, `codito.frontend.json`, or
`package.json#codito.frontend` with `cwd`, `devCommand`, and a loopback `baseUrl`.
Only then may it use a deterministic package-manager/framework fallback. Ambiguous
projects fail with `frontend_config_required`; MCP callers cannot supply a command,
working directory, port, absolute URL, browser executable, or profile path.

A ready configured loopback server may be reused but is never terminated by
Codito. A server started by Codito runs through the broker's native process-tree
containment and is released with the last session. Readiness has a bounded timeout,
stdout/stderr are bounded, and cleanup never uses kill-by-name.

Chromium uses a persistent project-specific profile below the agent data directory.
It never opens a user's Chrome/Edge profile. The browser revision is locked to the
Playwright Python package and shipped in the Windows payload; runtime downloads are
forbidden. The browser is visible so the local user can perform authentication
without sending credentials through MCP.

A transient transport reconnect preserves an established session for
the same account/device/project/grant/link owner. Connection epochs remain monotonic
request fences rather than session identity; older-epoch work is rejected, while a
newer epoch may recover the session. Revocation, ownership changes, expiry, long
disconnect, and shutdown still dispose it, and owner-bound stop remains available as
a cleanup-only operation after authority invalidation.

### Sessions, snapshots, and element identity

Version 1 permits one active browser session per project. Session authority is
bound to account, grant, link, device, project/root fingerprint, and the current
local security generation. Revocation, root replacement, timeout, agent shutdown,
or explicit stop closes it.

`frontend_session_start` accepts only an app-relative path without a query or
fragment, plus a bounded viewport.
Top-level navigation stays on the configured loopback origin. Popups, downloads,
external top-level navigation, arbitrary JavaScript/CDP, browser permissions,
clipboard access, and uploads are not public capabilities.

`frontend_snapshot` returns a Playwright viewport PNG as MCP image content plus a
bounded semantic element list, viewport geometry, and redacted console/network
issues. Durable journals replace the transient snapshot payload with a replay marker,
so pixels are never persisted there. Network summaries
omit query strings, headers, cookies, and bodies; console entries are length/count
bounded and pass through central redaction.

Every snapshot creates a new opaque `snapshot_id` and element registry. Element IDs
such as `e23` are meaningful only with that session and snapshot. Navigation, HMR,
and every action invalidate the registry. Inspect/source/action requests with stale
IDs fail instead of retargeting a different node. Coordinate inspection resolves a
node at a bounded viewport point and is subject to the same snapshot binding.

`frontend_inspect` returns browser-derived DOM identity, box geometry, selected
computed properties, matched rules, and accessibility state. It does not claim that
all browser DevTools data was returned. `frontend_act` exposes only click, hover,
focus, fill, allowlisted key press, bounded scroll, and option selection. Password
or credential-like fills are refused; authentication is a local-user action.

### Source mapping

CDP does not intrinsically know a React consumer callsite. Codito therefore reports
`exact`, `heuristic`, or `unavailable` rather than manufacturing certainty.

`@codito/vite-plugin-inspector` is an explicit development-only transform. It adds
`data-codito-source` to custom JSX component calls and
`data-codito-host-source` to intrinsic DOM hosts. Components must forward `data-*`
props for the consumer location to reach the host node. The transform has
`apply: "serve"`, so production builds contain neither attribute.

Page attributes are untrusted. The agent parses only the documented
project-relative `path:line:column` form, rejects traversal/absolute paths, resolves
through the registered project boundary, verifies the current file and coordinate,
and only then reports an exact *instrumented location*. This is not cryptographic
attestation that hostile page code did not copy a valid attribute to another node;
the source should still be read in context before mutation. The v0.3.0 adapter maps
consumer and DOM-host JSX only. Computed and matched CSS evidence is inspectable,
but mapping transformed stylesheets or design tokens back through source maps is
explicitly deferred; the protocol's `styles` collection is reserved and empty.

### Authorization and replay

Frontend reads require `projects:read + frontend:read`. Interaction requires
`frontend:interact`; session start additionally requires `shell:execute` because a
trusted project configuration can start native code, and source lookup additionally
requires `files:read`. These scopes are separate from `screen:read` and cannot be
inferred from an old browser/screenshot grant.

Outside explicit Full device access, session start requires one local approval that
shows the resolved config, route, viewport, native command, cwd, and loopback origin;
it is never persisted as Always allow. Browser ownership does not
elevate the process or bypass the Windows user's network/file authority.

Session start and actions are not automatically replayed after an uncertain device
outcome. Stop is idempotent. Snapshot, inspect, and source are read operations but
still require a current live session and snapshot binding.

## Consequences

The relay remains a routing/control plane; localhost and the browser profile never
leave the Windows device. Frontend diagnosis can correlate rendered pixels with
semantic, style, accessibility, console, network, and validated source evidence.

The Windows artifact is larger because it includes a matching Chromium revision.
This version deliberately does not add AI review, unrestricted selector execution,
desktop automation, React DevTools private-internals integration, cross-origin app
testing, or general managed-process/port tools.

Unit tests and a synthetic local browser fixture validate contracts and mechanics.
A release claim for a particular ODS application still requires the full installed
artifact scenario: start route, capture pixels/tree, inspect and hover the target,
resolve its TSX source, change it through normal file tools, observe HMR, and capture
the changed pixels.
