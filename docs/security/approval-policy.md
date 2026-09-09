# Local approval policy

Updated 2026-09-09. [ADR 0018](../adr/0018-focused-tools-and-local-access.md)
supersedes the earlier project-mode matrix. OAuth authorization and Windows
consent are separate; neither tool metadata nor model-supplied fields confer
local trust.

## Three selectable project modes

| UI mode / stored value | Inside project | External files, shell, screen/browser |
| --- | --- | --- |
| Ask every time / `native_approval` | Approval required | Approval required |
| Project access / `native_project` | Ordinary file operations and recognized project commands run without prompts | Approval required; eligible saved permissions may apply |
| Full device access / `full_access` | No local prompt | No local prompt |

Full access requires an explicit local warning/confirmation and grants the
logged-in user's native filesystem/network/desktop authority. It does not bypass
OAuth scopes, ownership, lock/secure-desktop protections, path validation,
operation limits or Windows ACLs; it provides no elevation.

Existing `native_trusted` is **not** upgraded to Full access. Its previous consent
did not authorize all screen/file operations. Legacy modes stay visible as legacy
until the user explicitly selects a new mode. `isolated` remains fail-closed:
the current broker does not prove AppContainer confinement.

Ask every time suppresses reuse and offering of persistent permissions for that
operation, without silently deleting the user's saved permissions.

## Native execution limits

PowerShell/CMD runs as the logged-in user. Cwd is not a security boundary.
Project access detects literal external paths and recognized dynamic/compound
syntax; unclassifiable recognized syntax requests review. Build tools, plugins,
scripts and child processes can perform transitive access which inspection of
the initial command cannot exhaustively detect. This is cooperative approval
routing, not an escape-proof sandbox. Project IDs select authority per call;
device-wide OAuth does not bind one conversation to one active project.

## Deny, Allow, Always allow

Approval delivery uses the tested native toast/COM action path plus the persistent
bottom-right fallback panel. Neither delivery mechanism raises the main Codito
window automatically. Actions carry one-use daemon-bound tokens; expiry, locking,
revocation, security-generation change and cancellation invalidate pending actions.

Deny or closing/expiry never authorizes. Allow permits the displayed operation.
Always allow appears only for capabilities with a defined persisted scope:

- File reads: same account/grant/link/device, origin project (new anchored calls),
  requested folder and root identity. Never permits writes or execution.
- Shell: exact command/executor/cwd/environment/executable identity and project/
  account/grant/link/device binding. It is not a wildcard for every command in a
  directory. Executable identity is rechecked; this is not handle-pinned execution.
- Screenshots: selected display configuration and account/grant/link/device. A
  changed topology requires fresh consent. Never permits browser/shell/file use.

Patch/deletion and browser-opening approvals remain one-shot outside explicit
Full access. A saved read permission cannot become a write permission.

## Managed frontend sessions

[ADR 0020](../adr/0020-managed-frontend-inspection.md) governs a separate
agent-owned browser capability. `screen:read`, `browser_open`, and their saved
permissions never authorize it. OAuth requires `frontend:read` for snapshots,
inspection and source evidence, `frontend:interact` for managed interaction, and
`shell:execute` when session start may run the configured project dev command.

In both Ask every time and Project access, session start requires one local approval
covering the resolved browser/config and potential native dev-server start. It is
not eligible for Always allow. Full device access remains an explicit local choice.
The MCP caller cannot provide or override the command, cwd, port, executable,
profile, or absolute URL. Reusing an already-ready loopback server does not grant
permission to terminate it.

Browser session authority is short-lived and bound to the same account/grant/link/
device/project/root and local security generation. It is not an Always allow grant.
Every interaction still rechecks those bindings. Password entry, uploads, clipboard,
downloads, external top-level navigation, raw selectors/JavaScript/CDP and browser
permission prompts are outside the approved surface.

Settings shows saved permission scope, capability, action identity and account/
link. Refresh, Revoke selected and Revoke category are local authenticated
operations. Revocation invalidates pending requests. Full access is an independent
project setting: removing a saved grant does not turn off explicit Full access.
Sign-out/re-enrollment clears the existing saved permissions.

## Approval details

Show the requesting account/link, project, target root/cwd, exact command or patch,
purpose, executable/environment details and risk reason. Never log OAuth secrets,
activation tokens, browser profile state or screenshot pixels. Main-window activation is not required
for approval or image capture. Generation/deadline checks remain mandatory before
data release or execution, including operations exempted from a local prompt.
