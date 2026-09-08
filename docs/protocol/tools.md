# MCP tool contracts

The public MCP endpoint exposes seven bounded tools. JSON Schema snapshots live in
[`packages/protocol/schemas`](../../packages/protocol/schemas) and are generated
from the Pydantic models in `codito_protocol`. Unknown fields are rejected.

Every successful tool response contains structured JSON plus a concise readable
summary. Every failure contains a stable `ErrorCode`, safe message, retryability,
correlation ID, and bounded structured detail. HTTP/MCP authentication errors also
carry the standards-required challenge metadata.

## Common invariants

- `project_id`, `job_id`, and `idempotency_key` are opaque URL-safe identifiers.
- Project paths use `/`, are relative to the selected project, and use `""` for root.
- Inputs cannot choose device/account or assert approval/trust. Those come from
  OAuth/link context and local Windows consent. `device_read.scope_path` explicitly
  requests an absolute directory; it does not register a project or grant access.
- `project_shell.execution=native_approval` requests one-shot native consent;
  `project_policy` uses the existing locally selected mode.
- All counts, byte sizes, timeouts, pages, and outputs have schema-enforced limits.
- The relay authorizes before dispatch; the agent independently checks the bound
  account/device/project/action digest and its local policy.
- Tunnel operations wrap input as `{"tool_name": ..., "input": ...}` and the action
  digest covers that complete wrapper, not only the inner input.

## `device_read`

Annotations: read-only, non-destructive, idempotent, open-world. Requires `files:read`
and local Windows consent. No `project_id` is needed: the authenticated device link
routes the request. `operation` is `list_directory` or `read_file`; `scope_path` is
the requested absolute local directory and `path` is relative to it. `purpose` is
mandatory. Listings are non-recursive, with `offset`/`limit` bounded by 10,000 scanned
entries and 500 returned entries. Files are at most 16 MiB, text-only; `start_line`,
`max_lines`, `max_bytes` bound the response. Results include names/types or numbered
text, encoding, newline style, size, SHA-256 and continuation/truncation fields.

An Always allow decision permits only reads with the same scope and local
account/grant/link/device/root identity. It cannot authorize patch or shell tools.
Unrequested directories are not silently registered. Reparse paths, placeholders,
hardlinks and protected Windows ACLs remain blocked. See ADR 0012.

## `project_read`

Annotations: read-only, non-destructive, idempotent, closed-world.

| Operation | Required scopes | Purpose |
|---|---|---|
| `list_projects` | `projects:read` | List authorized projects for this device link |
| `list_directory` | `projects:read files:read` | List bounded metadata below a relative directory |
| `read_file` | `projects:read files:read` | Read bounded numbered text and content metadata |
| `search_text` | `projects:read files:read` | Search bounded files/results under a relative path |

Because MCP tool metadata cannot vary by the discriminated operation, the advertised
tool-level scheme conservatively lists `projects:read files:read`. The server still
allows `list_projects` with only `projects:read` after validating the operation.

Representative inputs:

```json
{"operation":"list_projects","limit":50}
```

```json
{
  "operation": "read_file",
  "project_id": "project_01J123456789ABCDEF",
  "path": "src/codito/app.py",
  "start_line": 1,
  "end_line": 200,
  "max_bytes": 262144
}
```

`read_file` returns numbered text, detected encoding and newline style, total byte
size, whole-file SHA-256, returned line bounds, truncation, and a signed opaque
continuation cursor where needed. It never silently decodes malformed content.
Directory and search continuation cursors bind the account/device/project,
operation, query parameters, root identity, and snapshot/time; callers cannot edit
them to expand access.

## `project_apply_patch`

Required scopes: `projects:read files:read files:write`.
Annotations: state-changing, destructive, idempotent, closed-world.

```json
{
  "project_id": "project_01J123456789ABCDEF",
  "patch": "*** Begin Patch\n*** Update File: README.md\n@@\n-old\n+new\n*** End Patch\n",
  "base_hashes": {
    "README.md": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "idempotency_key": "patchkey_01J123456789ABCDEF",
  "dry_run": false
}
```

Every touched source and destination appears in `base_hashes`. A digest means the
file must exist and match exactly. `null` means it must not exist. The agent parses
and preflights all sections/hunks, rejects missing or ambiguous context, checks
local approval policy, writes a recovery journal, and only then commits the batch.
The result reports old/new hashes for each file and structured conflicts. A dry run
performs all checks but no file replacement.

Idempotency is scoped to account, OAuth grant, link/device, project, key, and action
digest. Reusing a key with different content returns `idempotency_conflict`.

See [anchored patch format](patch-format.md).

## `project_shell`

Required scopes: `projects:read shell:execute`.
Annotations: state-changing, destructive, non-idempotent, open-world.

Start a structured executable:

```json
{
  "action": "start",
  "project_id": "project_01J123456789ABCDEF",
  "working_directory": "",
  "purpose": "Run the unit test suite",
  "timeout_seconds": 300,
  "output_limit_bytes": 2097152,
  "idempotency_key": "shellkey_01J123456789ABCDEF",
  "command": {
    "kind": "exec",
    "executable": "uv",
    "arguments": ["run", "pytest", "-q"],
    "environment": {}
  }
}
```

Or provide an explicit non-interactive script:

```json
{
  "action": "start",
  "project_id": "project_01J123456789ABCDEF",
  "purpose": "Show repository status",
  "idempotency_key": "shellkey_01J123456789ABCDEG",
  "command": {"kind":"script","shell":"powershell","script":"git status --short"}
}
```

`poll` and `cancel` both require the original `project_id` and `job_id`; ownership
queries also include tenant, grant, link, and device. Poll resumes chunks strictly
after `sequence_cursor` and returns `next_sequence_cursor`, terminal state, exit
code, and truncation. Output chunks distinguish stdout/stderr/system.

No PTY, stdin, elevation, detachment, breakaway, or arbitrary GUI execution exists
through `project_shell`; bounded browser opening is a separate tool below. A start
that may have crossed the network boundary but lacks a durable start acknowledgment
returns `outcome_unknown`; it is never automatically retried.

## `project_manage`

Required scopes: `projects:read`; mutating operations also require `projects:write`.
Annotations: state-changing, destructive, idempotent, closed-world.

| Operation | Local behavior |
|---|---|
| `get_projects` | Returns opaque IDs and relay-safe metadata; never local roots |
| `request_add_project` | Queues a 15-minute Windows request; the user alone chooses the folder |
| `rename_project` | Requires one-shot local approval before changing the display title |
| `remove_project` | Requires one-shot local approval and unregisters metadata only |

`request_add_project` accepts a display title and idempotency key but deliberately
has no path field. The desktop app opens the native folder picker and registers the
root only after a local choice. `remove_project` never deletes the project folder or
its contents. Re-enabling a removed registration remains a local desktop action.

## `device_screenshot`

Requires `screen:read`. Device-only routing: no `project_id`, local path, remote
approval flag or arbitrary capture coordinates. An existing OAuth connection must
authorize this scope; a local Always allow cannot expand its OAuth grant.

List monitors without capturing pixels:

```json
{"action":"list_displays","purpose":"Choose the display to inspect"}
```

The response contains `action=list_displays`, `displays` and `topology_id`. Each
display has an opaque `id`, label, primary flag, width, height, scale factor,
identity fingerprint and `persistent_permission_supported`. At most 16 monitors
are returned. Display metadata requires the same OAuth scope but no pixel consent.

Capture one ID from that result:

```json
{
  "action": "capture",
  "display": "screen_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "purpose": "Inspect the requested monitor",
  "max_dimension": 1600
}
```

The example ID is illustrative: use the actual returned ID. `display=primary` and
`action=capture` are defaults. The chosen primary is resolved before consent; an
unavailable ID never falls back to another monitor. `max_dimension` is 640–2048.

Windows asks Deny / Allow / Always allow when persistent permission is supported.
Always allow covers only the selected logical display configuration, account,
grant, link and device. Changed topology requires approval again. Identical
serial-less hardware at the same port may keep its identity; this is not physical
hardware authentication. Revocation is available in local Settings. File/shell
permissions cannot authorize screenshots, and screen permissions cannot open a browser.

Capture refuses locked or non-Default/secure desktops. The PNG result includes its
display ID, width/height, timestamp and SHA-256. Image bytes are delivered as MCP
ImageContent, not text pretending to contain a picture. PNG is at most 600 KB;
base64 at most 800 KB. Durable SQLite/PostgreSQL journals omit image bytes; Redis
transport and downstream ChatGPT retention remain separate concerns. See ADR 0015.

## `device_desktop`

Requires `shell:execute` and separate one-shot Windows approval. Device-only tool;
only `action=open_browser` and `browser=default|firefox` are supported. It does not
offer general remote keyboard/mouse control or authorize actions from saved grants.

```json
{
  "action": "open_browser",
  "browser": "firefox",
  "url": "https://example.com/",
  "purpose": "Open the requested page in Firefox"
}
```

The URL is normalized HTTP/HTTPS, at most 4096 characters, without embedded
credentials, control characters, malformed escaping or file/custom URL schemes.
`purpose` is required and at most 1000 characters. The local approval displays the
exact URL/browser and warns that browsing uses the user's profile and sessions.
Firefox must be registered locally; no arbitrary executable, extra arguments or
fallback browser can be chosen by the model.

Success returns `action`, `browser`, normalized `url` and `status=submitted`.
It confirms only OS submission, not page load. A subsequent `device_screenshot`
call can inspect the selected screen subject to its independent permission. There
is no combined action that silently grants screenshot access or waits for navigation.

Offline devices reject before queuing. Requests possibly submitted without a
terminal result become non-retryable `outcome_unknown` and are never automatically
replayed. A caller should inspect the current screen before requesting a new open
after an uncertain outcome; a new request still needs local browser consent.

## Scope and annotation source of truth

The runtime must derive advertised `securitySchemes` and annotations from
`codito_protocol.TOOL_CONTRACTS` or assert exact equality in tests. Authorization is
still performed on every request; metadata alone is not enforcement.
