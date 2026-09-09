# MCP tool contracts

Updated 2026-09-09. The public catalog contains 37 focused tools. Existing
authenticated legacy names remain callable for cached clients but are not listed.
See [ADR 0018](../adr/0018-focused-tools-and-local-access.md).

| Tool | Action |
| --- | --- |
| `projects_list` | List registered projects and their locally configured mode |
| `project_add` | Request registration; the user chooses the folder in Windows |
| `project_rename` | Rename project metadata |
| `project_remove` | Remove registration, not the folder or its files |
| `directory_list` | List bounded directory entries |
| `file_read` | Read numbered text, hash, encoding and newline metadata |
| `text_search` | Search bounded text/globs with continuation |
| `file_patch` | Apply an exact anchored add/update/delete/move patch |
| `file_delete` | Delete one file with a required current hash |
| `execute_shell` | Execute an exact command string in PowerShell or CMD |
| `shell_status` | Resume job output by sequence cursor |
| `shell_cancel` | Cancel the specified running job |
| `screen_list` | List display metadata without capturing pixels |
| `screenshot_capture` | Capture the selected display and return MCP image content |
| `browser_open` | Open an HTTP/HTTPS URL in default browser or Firefox |
| `frontend_session_start` | Start/reuse a loopback dev server and open agent-owned Chromium |
| `frontend_snapshot` | Return a viewport PNG plus a snapshot-bound semantic UI tree |
| `frontend_inspect` | Inspect DOM identity, layout, selected CSS rules, and accessibility |
| `frontend_act` | Perform one bounded action on a snapshot-bound semantic element |
| `frontend_source` | Resolve validated project-relative consumer/DOM-host JSX evidence |
| `frontend_session_stop` | Close the browser and release agent-owned dev-server resources |
| `code_intelligence_status` | Report provider capabilities, setup and current analysis state |
| `code_workspace_summary` | Summarize languages, frameworks, manifests and build roots |
| `code_symbol_search` | Search bounded document/workspace symbols |
| `code_definition` | Resolve a definition at a source position |
| `code_references` | Find semantic references or explicitly structural fallback candidates |
| `code_implementations` | Find implementations when semantic/structural evidence supports them |
| `code_diagnostics` | Return version-aware diagnostics and readiness state |
| `code_hover` | Return signature/type/documentation hover content |
| `code_context` | Aggregate bounded source/navigation/diagnostic context from one snapshot |
| `code_call_hierarchy` | Traverse incoming/outgoing call relationships |
| `code_type_hierarchy` | Traverse super/subtype relationships |
| `code_impact` | Return bounded structural dependents/impact evidence |
| `code_architecture` | Return manifest/graph structural architecture |
| `code_dependencies` | Return manifest/graph dependency information |
| `code_related_tests` | Return structural/heuristic related-test candidates |
| `code_reindex` | Force incremental or full provider synchronization |

## Agent tool-selection policy

The public catalog is intentionally action-specific. ChatGPT should choose the
narrowest tool that directly represents the requested operation; reducing MCP
call count is not a reason to replace structured file operations with a shell
script. Independent read-only calls should run in parallel when the client can do
so.

| User intent | Preferred tool | Do not substitute |
| --- | --- | --- |
| Discover registered projects/IDs | `projects_list` | Invented IDs or paths |
| Discover filenames and directory metadata | `directory_list` | `dir`, `Get-ChildItem` |
| Read known file contents or obtain a mutation hash | `file_read` | `Get-Content`, `type` |
| Find arbitrary text or regex matches | `text_search` | `Select-String`, `findstr` |
| Find symbols/definitions/references/relationships | Matching `code_*` tool | Text or shell approximation |
| Add, update, move, or batch-delete text files | `file_patch` | Shell redirection or mutation scripts |
| Delete one hash-verified file | `file_delete` | `del`, `Remove-Item` |
| Build, test, format, run Git/package tools/Docker/SSH/programs | `execute_shell` | File tools cannot execute processes |
| Continue or stop an existing process | `shell_status` / `shell_cancel` | Resubmitting `execute_shell` |
| Inspect a Windows display | `screen_list` / `screenshot_capture` | Shell screenshot utilities |
| Open an HTTP/HTTPS page | `browser_open` | Shell process launch |
| Inspect or interact with a local web UI | Matching `frontend_*` tool | `browser_open`, desktop screenshots, raw selectors, or shell browser automation |

The MCP initialize response carries the same routing rules plus concrete examples.
Each tool and every public input property has a concise description explaining
what it means, when the tool should be selected, and relevant side effects. The
relay contract tests reject metadata regressions, including missing parameter
descriptions or a Shell contract that omits the dedicated file-tool boundary.

For example, inspecting three known files, finding a filename, and locating a
source symbol should use parallel `file_read` calls, `directory_list`, and the
appropriate search/code tool. It should not bundle `Get-Content`,
`Get-ChildItem`, and `Select-String` into one PowerShell command. `execute_shell`
remains correct for real builds, test execution, Docker, SSH, Git, and other
process-based work.

## Identity, authorization and metadata

Every content/action tool requires an opaque `project_id` and a human-readable
`purpose` (job status/cancellation use job identity). Device-only project/display
listing has no project context. OAuth account/link/device/project checks and
capability scopes always apply, including Full device access.

Project ID selects authority **per call**. The current device-wide OAuth grant
is not a conversation-bound single-project grant. A caller can select another
registered project it is authorized to use. An explicit external target under
another project does not change the origin project's local policy.

Each descriptor includes an action-specific title, agent-oriented description,
fully described input fields, accurate safety annotations,
OAuth security schemes and static short invoking/invoked status strings.
ChatGPT controls its own narrative and host approval UI. Metadata cannot force a
custom sentence for every runtime argument. After a schema change, a client with
cached tools may need a tool refresh; the old callable aliases remain available.

Every public tool declares its own exact `outputSchema`, including its typed
`result` payload and the common success/error envelope. For example, file reads
advertise numbered text/hash/continuation fields, shell start advertises job state,
and screenshots advertise image metadata while PNG bytes travel separately as MCP
`ImageContent`. Returned `structuredContent` is validated by the protocol models.
The MCP `initialize` response publishes the Codito release SemVer as
`serverInfo.version`; a new release therefore exposes a new handshake version.

## Files

`path` and all patch paths are relative to the project root. Optional
`scope_path` explicitly requests an absolute Windows directory as the target
root. The agent validates/pins it locally; it never registers it automatically.
Within that scope, all file paths remain relative and reject traversal, ADS,
device/UNC aliases, reparse points, hardlinked mutations and unsupported roots.
The user's Windows ACLs still apply; these tools do not elevate.

External access follows the selected local project policy. Read-only saved
folder grants cannot authorize writes, deletes, shell or screenshots.

`file_read` accepts `start_line`, `max_lines`, `max_bytes` and a continuation.
Its whole-file SHA-256 is the precondition for patch/delete. UTF-8 and BOM-marked
UTF-16 can be read; text patches currently require UTF-8. Limits and complete
schemas are generated from `codito_protocol.facade`, not duplicated by hand.

`file_patch` takes `patch`, `base_hashes`, `idempotency_key`, `dry_run`.
A hash means the existing file must match; null means the destination must not
exist. All source and move destination paths must be covered exactly.

```text
*** Begin Patch
*** Update File: src/example.py
@@
-def greeting():
-    return "old"
+def greeting():
+    return "new"
*** Add File: notes.txt
+New file
*** Delete File: obsolete.txt
*** End Patch
```

Update sections may contain `*** Move to: relative/destination` before their
hunks. Each hunk begins with `@@`, contains exact context/removal lines and
replacement lines. Ambiguous or missing anchors fail; there is no fuzzy match.
Parent directories must already exist. Required hashes prevent silent overwrite.
All hunks preflight before mutation; a local journal provides batch rollback/
crash recovery. External reads retain directory handle pins. Atomic replacement
releases those read pins and immediately revalidates root/parent/target identities,
so it is not a handle-relative OS transaction invisible to other native processes.
Idempotency binds account/grant/link/device/project and target scope.

`file_delete` accepts one `path`, `base_hash`, `idempotency_key` and
`dry_run`; internally it uses the same journaled patch engine.

## Shell

```json
{
  "project_id": "opaque-project-id",
  "command": "dotnet build",
  "executor": "powershell",
  "cwd": "",
  "timeout_seconds": 300,
  "purpose": "Build the selected project",
  "idempotency_key": "unique-build-request-001"
}
```

No action enum or preselected command recipe is required. `executor` is
`powershell` (default) or `cmd`. Empty cwd means project root; a relative cwd
stays below it; an absolute cwd requests outside access. Execution timeout is
1–1800 seconds. The initial response waits up to 30 seconds for output; a
nonterminal response returns a job ID for `shell_status`, not a reason to resend
the command. Output is bounded and sequenced; `shell_cancel` cancels that job.
Approval waiting has a separate bounded timeout.

Native commands use the installed Windows environment. Cwd is not a sandbox.
Known external paths and recognized indirect/compound commands prompt in Project
access; transitive behavior of arbitrary build tools cannot be exhaustively
classified. Full device access is an explicit local opt-in to native user
authority. No remote field can assert approved/trusted or change local policy.
Commands are never automatically replayed after an uncertain start.

## Code Intelligence

All `code_*` tools use project-relative source coordinates and the currently registered
project identity. The public schema is language-neutral. Provider details stay local to
the Windows agent.

`code_intelligence_status` and `code_workspace_summary` are non-executing reads. The
remaining analysis tools may start a trusted local language/graph process and therefore
also require `shell:execute` plus the project's native-execution approval policy.

Responses carry provider (`lsp`, `graph`, `manifest`, or unavailable), precision,
snapshot ID, captured/verified time, freshness, completeness and warnings. An LSP result
is exact only for the operation actually returned by that server. Structural graph
fallback is always identified as structural.

Freshness does not depend on Git or a manual refresh: Codito content-hashes the bounded
eligible workspace, synchronizes provider state on add/edit/delete/rename, and verifies
the snapshot again after the query. Repeated concurrent changes return a retryable race
rather than stale success. `code_reindex` remains available for explicit incremental or
full refresh.

See [ADR 0019](../adr/0019-code-intelligence.md) and the
[operations guide](../operations/code-intelligence.md).

## Screens and browser

`screenshot_capture` chooses `display` (primary or ID from `screen_list`),
`max_dimension`, `purpose`, and project context. It still requires
`screen:read` OAuth scope. Outside Full device access, a selected-display local
permission is required; Ask every time does not reuse saved permission.
Capture while locked/on a secure desktop is refused in every mode. Actual PNG
pixels are returned as MCP ImageContent, not a resource URL. Durable history omits
pixels, so a replay may honestly report that image bytes were not retained.

`browser_open` takes `url`, `browser`, project context and purpose.
It uses `shell:execute` OAuth scope and separate local browser approval unless
Full device access was explicitly selected. It reports submission to Windows,
not proof the page loaded.

## Managed frontend browser

The six `frontend_*` tools route through hidden `project_frontend` but remain
action-specific in the public catalog. They run Playwright/Chromium on the Windows
agent next to the registered project and its loopback dev server; localhost is not
published by the relay.

`frontend_session_start` takes `project_id`, `purpose`, an app-relative `route`, a
320–2048 by 240–2048 `viewport`, a 5–120 second readiness timeout, and an
`idempotency_key`. It requires `projects:read + frontend:interact + shell:execute`.
Command, cwd, base URL, browser executable, profile, and port are local
configuration—not remote parameters. One project has at most one active session.

`frontend_snapshot` returns a PNG as MCP ImageContent plus `snapshot_id`, sanitized
URL/title, viewport, up to 500 bounded elements (`e1`, `e2`, …), console entries,
failed HTTP/network summaries, truncation state, and warnings. Each element includes
tag, role/name/text, viewport box, visibility, enabled/focus state, classes, and an
optional semantic parent. Image bytes are removed from structured results; durable
history keeps only a non-replayable image marker.

`frontend_inspect` requires the current `snapshot_id` and exactly one target:
`element_id` or viewport `x`/`y`. It returns the resolved element, selected computed
styles, matched declarations and accessibility fields. The API has no CSS selector,
XPath, JavaScript, or raw CDP input.

`frontend_act` requires the current snapshot/element, an idempotency key and exactly
the fields appropriate to `click`, `hover`, `focus`, `fill`, `press`, `scroll`, or
`select`. Press uses an enumerated key; scroll is bounded; password, file and
credential-like inputs are refused. Completion invalidates the snapshot. Start and
act are replay-unsafe after an uncertain device result; clients must not blindly
resubmit them.

`frontend_source` returns only validated project-relative locations with
`exact`, `heuristic`, or `unavailable` confidence. Exact React/ODS consumer mapping
uses development-only metadata from `@codito/vite-plugin-inspector`; the agent does
not trust the page attribute until normal project path and coordinate checks pass.
The v0.3.0 adapter maps consumer and DOM-host JSX only; its `styles` field is reserved
and empty. Matched/computed CSS values are still available through
`frontend_inspect`, while stylesheet/token source maps remain future work. Source
lookup requires `files:read` in addition to frontend read scopes.

`frontend_session_stop` is idempotent. Session ownership is bound to account, grant,
link, device, project/root, and local security generation. The connection epoch fences
stale transport requests but may advance for the same stable owner after a transient
reconnect. Session expiry, HMR/navigation staleness, revocation, a long disconnect,
and shutdown fail closed; stop may always perform owner-bound cleanup on a current
transport even after session authority expires.
See [ADR 0020](../adr/0020-managed-frontend-inspection.md) and the
[operations guide](../operations/frontend-inspection.md).

## Compatibility

Hidden aliases: `project_read`, `project_apply_patch`, `project_shell`,
`project_manage`, `project_frontend`, `device_read`, `device_screenshot`,
`device_desktop`.
They use the same authorization and execution services. Unanchored legacy device
operations keep their existing separate approval flow and never inherit a
project's Full access setting.
