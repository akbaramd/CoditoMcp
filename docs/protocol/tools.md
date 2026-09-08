# MCP tool contracts

Updated 2026-09-09. The public catalog contains 15 focused tools. Existing
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

## Identity, authorization and metadata

Every content/action tool requires an opaque `project_id` and a human-readable
`purpose` (job status/cancellation use job identity). Device-only project/display
listing has no project context. OAuth account/link/device/project checks and
capability scopes always apply, including Full device access.

Project ID selects authority **per call**. The current device-wide OAuth grant
is not a conversation-bound single-project grant. A caller can select another
registered project it is authorized to use. An explicit external target under
another project does not change the origin project's local policy.

Each descriptor includes an action-specific title, accurate safety annotations,
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

## Compatibility

Hidden aliases: `project_read`, `project_apply_patch`, `project_shell`,
`project_manage`, `device_read`, `device_screenshot`, `device_desktop`.
They use the same authorization and execution services. Unanchored legacy device
operations keep their existing separate approval flow and never inherit a
project's Full access setting.
