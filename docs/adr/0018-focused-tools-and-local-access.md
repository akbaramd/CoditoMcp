# ADR 0018 — Focused MCP tools and explicit local access policy

Date: 2026-09-08. Status: accepted and implemented.

## Decision

Expose focused project-management, file, shell, display and browser tools. Keep
the existing authenticated internal wire names as compatibility aliases, hidden
from the new tools/list response. `execute_shell` accepts a command string,
PowerShell/CMD executor and timeout; status and cancellation are separate tools.
Patching retains exact anchors, required base hashes, bounded input and a local
recovery journal. External file targets require an explicit absolute scope root;
file names inside that scope remain relative and traversal-checked.

Human-readable `title` and static `openai/toolInvocation/invoking` / `invoked`
metadata describe the actual action. These status strings are at most 64
characters. They do not override ChatGPT's own narration or approval interface.

Every public tool description states when to use that tool and distinguishes it
from adjacent tools. Every public input property carries a model-readable
description. Server instructions define a deterministic preference order:
dedicated file and semantic-code tools precede Shell; independent reads may run
in parallel; Shell is reserved for actual process execution such as builds,
tests, Git, package managers, Docker, and SSH. Examples live in server
instructions rather than bloating individual tool descriptions. Hidden legacy
aliases do not appear in the public catalog and therefore cannot compete during
fresh tool selection.

Three locally selected project policies are displayed:

| Policy | Inside project | Outside project / desktop |
| --- | --- | --- |
| Ask every time | Prompt | Prompt |
| Project access | No prompt for recognized local access | Prompt; eligible saved permissions may apply |
| Full device access | No local prompt | No local prompt |

No project is upgraded automatically. Full access uses a new `full_access` value;
legacy `native_trusted` is not silently converted to screenshot/file authority.
Legacy isolated configuration remains blocked until explicitly changed locally.
OAuth ownership, resource audience,
scopes, lock-state checks, input validation and operating-system permissions
remain mandatory in every mode. Full device access does not confer elevation.

## Security limits that must stay visible

Native command execution is not OS filesystem isolation. Working directory and
command text cannot prove the behavior of scripts, build tools or child
processes. Recognized external references and uncertain shell constructs prompt;
the UI and tool descriptions must not advertise this as an escape-proof sandbox.
Always-allow shell grants bind the exact execution identity, not just a directory.
Changing the command cannot reuse an unrelated saved command permission.

`project_id` selects a locally configured project's authority for each call. The
device OAuth grant currently authorizes all its registered projects; it is not
an authenticated conversational "active project". An external path under another
project still prompts under Project access, but a caller can explicitly select
another authorized project. Do not claim otherwise. Grant-bound single-project
links would be a separate authorization feature.

File patch journals provide recoverable batches, not an OS transaction invisible
to concurrent native applications. Existing conservative path/identity checks
remain in force; no fuzzy patching or silent overwrite is introduced.
External reads retain handle pins for their complete operation. Atomic replacement
requires releasing directory read pins; external mutation therefore revalidates
root, parent and target identities immediately before each change but cannot claim
an unobservable handle-relative transaction. A native broker enhancement is needed
to eliminate that final race window.

## Primary research (accessed 2026-09-08)

- [Build an MCP server](https://developers.openai.com/plugins/build/mcp-server):
  focused action-oriented tools, explicit schemas and server-side authorization.
- [Optimize metadata](https://developers.openai.com/plugins/guides/optimize-metadata):
  clear names, descriptions and accurate safety annotations.
- [Apps reference](https://developers.openai.com/plugins/reference): tool titles,
  securitySchemes and the two short invocation-status metadata fields.

## Verification required before release

- Public list/call schemas, hidden legacy compatibility, metadata and OAuth gates.
- Three-mode matrix for files, native shell, screenshots and browser launch.
- Exact-command Always allow, individual revocation and no authority escalation.
- External scope hash binding, deny-before-read/write and journal recovery.
- Existing toast, screenshot, OAuth and connection resilience regressions.
