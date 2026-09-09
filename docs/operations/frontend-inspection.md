# Managed frontend inspection

Codito 0.3.0 runs a dedicated Chromium session on the Windows agent so an MCP
client can inspect the same `localhost` application that a developer sees. This
workflow is separate from opening the user's browser or capturing a Windows
display.

## Prerequisites

1. Update the relay and Windows agent together and refresh the connector's tool
   catalog.
2. Authorize `projects:read`, `frontend:read`, `frontend:interact`, `files:read`,
   and `shell:execute`. Keep the scopes narrower if source lookup, interaction, or
   server startup is not needed.
3. Register the project locally. The browser and dev server run with the logged-in
   Windows user's authority and never expose localhost through the relay.

The release ZIP contains the browser revision matched to Playwright. The agent does
not download a browser at runtime and never reuses a personal Chrome or Edge profile.

## Configure the development server

Explicit configuration wins. Add `.codito/frontend.json` inside the registered
project:

```json
{
  "cwd": ".",
  "devCommand": "pnpm dev",
  "baseUrl": "http://localhost:5173"
}
```

For a nested .NET frontend, for example:

```json
{
  "cwd": "src/Web",
  "devCommand": "dotnet run",
  "baseUrl": "http://localhost:5080"
}
```

`codito.frontend.json` and `package.json#codito.frontend` accept the same three
fields. Paths are project-relative and `baseUrl` must be a bare HTTP loopback
origin. HTTPS development origins are deferred until Codito can validate a trusted
local CA without disabling certificate verification. Remote callers cannot override
the command, cwd, port, executable, profile, or origin.

Without explicit configuration, Codito accepts only an unambiguous root
`package.json` with a `dev` or `start` script, recognizes the lockfile's package
manager, and selects a conventional Vite, Angular, Next.js, or react-scripts port.
All other layouts fail with `frontend_config_required` so the wrong command is not
guessed.

If the configured origin is already ready, Codito reuses it and never terminates
it. Otherwise it owns the new dev-server process tree and stops it with the final
frontend session.

## Enable exact TSX source mapping

Install the release artifact for `@codito/vite-plugin-inspector` (or use the package
from this repository while developing) and add it to the Vite config:

```ts
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { coditoInspector } from "@codito/vite-plugin-inspector";

export default defineConfig({
  plugins: [
    react(),
    coditoInspector({
      projectRoot: fileURLToPath(new URL(".", import.meta.url)),
    }),
  ],
});
```

The plugin is restricted to Vite's development server. Custom components must
forward `data-*` props to their DOM host:

```tsx
export function Button(props: React.ComponentProps<"button">) {
  return <button {...props} />;
}
```

The browser page is not trusted to name arbitrary files. The agent accepts a source
location only after resolving it inside the registered project and verifying its
line and column. Missing forwarding returns `heuristic` or `unavailable`, not a
fabricated exact match.

In 0.3.0 this adapter maps the JSX consumer and DOM-host implementation. Use
`frontend_inspect` for computed and matched CSS evidence; stylesheet and design-token
source-map resolution is intentionally deferred, so `frontend_source.styles` is
empty in this release.

## Tool sequence

Start one project route and keep the returned `session_id`:

```text
frontend_session_start(
  project_id="<project>",
  route="/demos/dashboard",
  viewport={"width": 1440, "height": 900},
  ready_timeout_seconds=45,
  idempotency_key="<unique>",
  purpose="Review dashboard actions"
)
```

The initial route is a plain app-relative path. Query strings and fragments are
rejected; reach parameterized or authenticated state through the visible local UI.

Then capture pixels and semantic state together:

```text
frontend_snapshot(project_id="<project>", session_id="<session>",
                  max_elements=500, purpose="Capture current UI")
```

The result contains a PNG, `snapshot_id`, and bounded elements such as `e23` with
role/name/box state. Always pass both IDs to subsequent calls:

```text
frontend_inspect(..., snapshot_id="<snapshot>", element_id="e23")
frontend_source(..., snapshot_id="<snapshot>", element_id="e23")
frontend_act(..., snapshot_id="<snapshot>", element_id="e23",
             action="hover", idempotency_key="<unique>")
```

Inspection can alternatively use `x` and `y` from the same viewport snapshot.
Actions invalidate the element registry, so take a new snapshot before the next
inspect/source/action. This prevents HMR or navigation from silently retargeting a
stale `e23`.

The 0.3.0 element registry covers the main document. An embedded iframe remains
visible in the PNG, but its internal document is not exposed for semantic targeting;
the snapshot reports a warning when frame documents were omitted.

Supported actions are `click`, `hover`, `focus`, `fill`, `press`, `scroll`, and
`select`. Keys are allowlisted, scroll deltas are bounded, and password fills are
rejected. Login is performed by the local user in the visible dedicated browser.

Always finish with:

```text
frontend_session_stop(project_id="<project>", session_id="<session>",
                      purpose="Frontend review complete")
```

Stop is idempotent. Security changes, project-root replacement, expiry, and agent
shutdown also close the browser and owned server.

## What leaves the device

Snapshot pixels and bounded semantic/style/accessibility evidence return through
MCP. Network summaries omit queries, headers, cookies, and bodies; console entries
are redacted and capped. Browser cookies, local storage, profile paths, absolute
source paths, raw DOM attributes, and dev-server output are never returned. Durable
agent/relay history keeps only an image replay marker, not the snapshot payload.

See [ADR 0020](../adr/0020-managed-frontend-inspection.md), the
[tool contracts](../protocol/tools.md), and the
[threat model](../security/threat-model.md).
