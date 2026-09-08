# MCP tool contracts

The public MCP endpoint exposes four bounded tools. JSON Schema snapshots live in
[`packages/protocol/schemas`](../../packages/protocol/schemas) and are generated
from the Pydantic models in `codito_protocol`. Unknown fields are rejected.

Every successful tool response contains structured JSON plus a concise readable
summary. Every failure contains a stable `ErrorCode`, safe message, retryability,
correlation ID, and bounded structured detail. HTTP/MCP authentication errors also
carry the standards-required challenge metadata.

## Common invariants

- `project_id`, `job_id`, and `idempotency_key` are opaque URL-safe identifiers.
- Paths use `/`, are relative to the selected project, and use `""` for root.
- Inputs cannot name a device, account, absolute root, execution mode, approval, or
  trust flag. Those come from the OAuth/link context and local project policy.
- All counts, byte sizes, timeouts, pages, and outputs have schema-enforced limits.
- The relay authorizes before dispatch; the agent independently checks the bound
  account/device/project/action digest and its local policy.
- Tunnel operations wrap input as `{"tool_name": ..., "input": ...}` and the action
  digest covers that complete wrapper, not only the inner input.

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

No PTY, stdin, elevation, detachment, breakaway, or GUI execution exists. A start
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

## Scope and annotation source of truth

The runtime must derive advertised `securitySchemes` and annotations from
`codito_protocol.TOOL_CONTRACTS` or assert exact equality in tests. Authorization is
still performed on every request; metadata alone is not enforcement.
