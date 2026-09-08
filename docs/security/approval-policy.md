# Local approval policy

Updated 2026-09-08: [ADR 0013](../adr/0013-native-project-approvals.md) extends the
original matrix below with opt-in native_project, separate persistent shell grants,
non-consuming approval delivery and one-use native-toast action tokens.
[ADR 0014](../adr/0014-consented-screenshots.md) adds screen consent; [ADR 0015](../adr/0015-selected-display-and-browser-consent.md)
extends it with selected-display Always allow and separate one-shot browser opening.
These are implemented cooperative native policies, not a completed AppContainer sandbox.

Authorization and approval are separate. OAuth answers whether a remote client may
request a capability. The Windows policy/UI answers whether this exact device action
may execute under the selected local mode.

## Project modes

| Mode | Isolation | Prompt | Authority statement |
|---|---|---|---|
| `isolated` (default) | Verified AppContainer + Job Object, project grant only, no network | None for allowed project-only actions | Bounded by tested sandbox and handle policy |
| `native_approval` | Logged-in user token | One-shot for each shell/native-sensitive action | Full user authority for the approved process |
| `native_project` | Cooperative native policy, not a sandbox | None for ordinary project work; explicit outside cwd/declared or recognized external paths require consent | Full user authority; arbitrary/dynamic accesses cannot be exhaustively detected |
| `native_trusted` | Logged-in user token | None after explicit local opt-in warning | Full user filesystem/network authority; escape cannot be reliably detected |

Sandbox failure always returns `sandbox_unavailable`; it never silently falls back.
A caller can explicitly request `project_shell.execution=native_approval` for a
one-shot native run using installed Windows tools. This does not change project mode.

## Decision matrix

| Capability/risk | Isolated | Native approval | Native trusted | Session grant eligible? |
|---|---|---|---|---|
| List projects/directories | Allow | Allow | Allow | Yes, read-only |
| Read/search normal project files | Allow | Allow | Allow | Yes, read-only |
| Suspected secret file | Local deny/prompt per policy | One-shot or deny | Local warning/policy | **No** |
| Add/update normal project file | Allow after hash/policy | One-shot if native implementation needed | Allow | Only isolated, non-secret, non-delete |
| Delete/move-overwrite | One-shot | One-shot | Local configured behavior | **No** |
| Isolated shell without network | Allow only after broker self-test | N/A | N/A | At most narrowly similar isolated action |
| Native shell | N/A | One-shot | Allow | **No** |
| External read via `device_read` | Windows consent independent of project mode | Same | Same | Separate persistent **read-only** folder grant available |
| External write | Not via isolated tools | Possible through explicitly approved native command | Possible through trusted command | **No** |
| Network capability | Deny in isolated v1 | Consequence of one-shot native command | Consequence of trusted command | **No** |
| Elevation/arbitrary GUI/PTY/detach | Deny | Deny | Deny through shell API | **No** |
| Screen metadata | `screen:read`; no pixels | Same | Same | No pixel permission is granted |
| Capture selected display | Separate local consent | Same | Same | Separate saved display permission available |
| Open HTTP/HTTPS URL in default browser/Firefox | Separate one-shot local consent | Same | Same | **No**, including saved file/shell/screen grants |

`native_project` extends the original matrix: ordinary project reads and anchored
writes/deletes/moves do not prompt; native commands follow the cooperative routing
described above. Saved shell scope permissions are independent of read permissions.
No claim is made that a native process is confined to its working directory.

## Approval request display

A new request displays an actionable Windows notification without automatically
raising the Codito window or a modal dialog. Eligible native notifications offer
`Deny`, `Allow`, `Always allow`; one-shot-only capabilities offer `Review details`
instead of Always. Native toast actions may approve only through a valid one-use
daemon-bound token. When notifications are suppressed/unavailable, requests remain
pending and can be reviewed from the local Overview/tray.

The user explicitly opens **Review pending approvals** or the notification's
review action to see a dialog. It shows:

- requesting account and OAuth link/grant label;
- device and project title plus local root (visible locally only);
- exact patch or command and canonical action digest suffix;
- resolved executable and working directory;
- environment values added/removed/redacted, not inherited secrets;
- requested paths, network implication, execution mode, timeout/output cap;
- reason for the prompt and explicit authority/risk statement.

The dialog defaults to `Deny`. It offers `Allow once` and only policy-eligible
session/persistent options. Closing or expiration does not approve; display/IPC
failure cannot imply consent. Pending actions remain subject to security-generation
and deadline checks.

For `device_read`, Windows offers `Always allow reading this folder`.
It covers the displayed directory and descendants, including private data the
Windows user can access. It is bound to account/grant/link/device/root identity,
survives restart/reconnect, and can be revoked under Settings. It does not grant
editing, deletion, execution, network operations, or elevation. Sign-out and
re-enrollment remove saved permissions. See ADR 0012 for the changed path contract.

For `device_screenshot`, Always allow is separate and permits future captures of
the selected display configuration, including private visible content, without
another prompt. Its key binds account/OAuth grant/link/device, `screen:read`, display
ID and identity/topology. It does not grant all-screen capture, browser actions,
files or shell. `primary` is resolved to a specific display before authorization.
Changed layout/scale/primary configuration requires fresh approval. A serial-less
monitor replaced with identical hardware on the same port can retain its logical
identity; Always allow does not authenticate a unique physical monitor.

Settings → saved screenshot permissions → **Revoke all screenshot permissions**
removes only screen grants and invalidates pending approvals. Sign-out/re-enrollment
removes saved permissions. Capture remains disabled while locked or on a secure
desktop even when a matching saved permission exists.

For `device_desktop`, `shell:execute` only authorizes requesting browser opening.
The local `desktop:open_url` prompt always asks for one-shot consent and names the
exact browser/URL, network contact and use of existing browser sessions. Neither
saved native shell permission nor saved screen permission can satisfy it.

## Decision binding

The daemon accepts decisions through the authenticated local named pipe. Native
toast callbacks additionally carry a random per-button 256-bit token resolved to
the immutable pending request and decision. Request state binds:

`request_id, action_digest, account_id, grant_id, link_id, device_id, project_id,
capability, connection_epoch, decision, deadline, security_generation`.

Per-button tokens are consumed atomically before execution. The callback contains
no authority to create a request or extend its deadline; mismatched, expired or
replayed activations fail closed. Native notifications use the registered COM
activator, not Windows URL-scheme launching. A relay/model never overrides a local
decision or repairs a rejected activation by asserting `approved=true`.

## Session grants

A session grant is no broader than account + OAuth grant + device + project +
capability + normalized action-family digest + connection epoch. It expires after
30 minutes absolute or 10 minutes idle, whichever comes first. It clears on Windows
lock, sleep, logout, agent/UI restart, grant/link/device revocation, project identity
change, or relay disconnect exceeding 60 seconds.

“Similar” must be deterministically defined (for example, the same read-only
operation under the same project subtree), shown before approval, and tested. It
cannot be inferred from natural-language purpose.
