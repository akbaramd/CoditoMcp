# Local approval policy

Authorization and approval are separate. OAuth answers whether a remote client may
request a capability. The Windows policy/UI answers whether this exact device action
may execute under the selected local mode.

## Project modes

| Mode | Isolation | Prompt | Authority statement |
|---|---|---|---|
| `isolated` (default) | Verified AppContainer + Job Object, project grant only, no network | None for allowed project-only actions | Bounded by tested sandbox and handle policy |
| `native_approval` | Logged-in user token | One-shot for each shell/native-sensitive action | Full user authority for the approved process |
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
| Elevation/GUI/PTY/detach | Deny | Deny | Deny through Codito API | **No** |

## Approval request display

A Windows toast is only an attention mechanism. It opens a persistent signed dialog;
toast buttons cannot approve. The dialog shows:

- requesting account and OAuth link/grant label;
- device and project title plus local root (visible locally only);
- exact patch or command and canonical action digest suffix;
- resolved executable and working directory;
- environment values added/removed/redacted, not inherited secrets;
- requested paths, network implication, execution mode, timeout/output cap;
- reason for the prompt and explicit authority/risk statement.

Buttons are `Deny`, `Allow once`, and—only when policy marks it eligible—`Allow
similar access for this session`. Closing, timeout, lock, or IPC failure denies.

For `device_read` only, Windows also offers `Always allow reading this folder`.
It covers the displayed directory and descendants, including private data the
Windows user can access. It is bound to account/grant/link/device/root identity,
survives restart/reconnect, and can be revoked under Settings. It does not grant
editing, deletion, execution, network operations, or elevation. Sign-out and
re-enrollment remove saved permissions. See ADR 0012 for the changed path contract.

## Signed decision binding

The daemon accepts a decision only when it authenticates the UI process/channel and
the signature/MAC binds:

`request_id, action_digest, account_id, grant_id, link_id, device_id, project_id,
capability, connection_epoch, decision, issued_at, expires_at, nonce`.

The daemon recomputes the action digest from the pending immutable request. One-shot
nonces are consumed atomically before execution. A mismatch is an approval replay
event and never asks the relay/model to override it.

## Session grants

A session grant is no broader than account + OAuth grant + device + project +
capability + normalized action-family digest + connection epoch. It expires after
30 minutes absolute or 10 minutes idle, whichever comes first. It clears on Windows
lock, sleep, logout, agent/UI restart, grant/link/device revocation, project identity
change, or relay disconnect exceeding 60 seconds.

“Similar” must be deterministically defined (for example, the same read-only
operation under the same project subtree), shown before approval, and tested. It
cannot be inferred from natural-language purpose.
