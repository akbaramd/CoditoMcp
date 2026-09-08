# ADR 0013 — Cooperative native project policy and durable approval delivery

Accepted 2026-09-08. Extends ADR 0003 and the original approval matrix following
the owner's explicit request for installed Windows tools, prompt-free project work,
and persistent approval for outside-project shell requests.

## Decision

Add opt-in `native_project` (Windows UI: **Project access (native)**). Existing
projects are not silently downgraded from isolation/one-shot policies. Enabling
this mode requires the full-user-authority acknowledgement in Windows/CLI.

- Project reads and anchored writes/deletes/moves remain project-relative.
- Ordinary native commands inside the project need no prompt. They inherit the
  installed user/machine tool PATH, standard developer environment and Job Object.
- External cwd, declared outside paths, and recognized literal external/parent
  paths trigger approval. `external_working_directory` is explicit and separate
  from project-relative `working_directory`. Cwd validation is pinned with Windows
  handles until the command ends; only the daemon sets the broker's authorization bit.
- This is **cooperative approval routing, not an OS sandbox**. Arbitrary programs,
  dynamically constructed paths, loaded code and child processes can access all
  resources allowed to the Windows user. Network access is native user authority.
  Literal scanning must never be described as exhaustive escape detection.
- `isolated` still fails closed. Legacy `native_trusted` still means unrestricted
  native authority. `execution=native_approval` forces a new prompt even if a
  matching saved native permission exists; it never changes the project mode.

Shell starts persist a job before returning `pending_approval`. Windows consent
gets 15–300 seconds (default 180), separate from the 1–1800 second process timeout.
Poll/cancel remain bound to account, OAuth grant, link, device and project. Duplicate
starts reuse the same pending job. Denial, cancellation, revocation/security-generation
change or disconnection prevents a pending command from starting.

**Always allow shell** is separate from read permissions. Its key binds account,
OAuth grant, link, device, project, project root identity, external-cwd identity
where applicable, and the exact canonical requested scope. It authorizes future
native commands for that request scope, not filesystem confinement. It survives
restart; different scopes ask again. Settings can revoke shell grants independently.
Sign-out/re-enrollment revoke saved shell and read permissions.

## Delivery and native actions

The authenticated named pipe peeks pending requests instead of consuming them on
delivery. Tray restart/display errors therefore cannot lose a still-pending request.
The UI explicitly acknowledges display. Overview/tray expose Review pending approvals.
A diagnostic test exercises the same display queue without reading/executing/granting.

Actionable WinRT ToastGeneric notifications use Codito's own per-user AppUserModelId
and `codito-approval` protocol handler. Buttons are Deny, Allow, Always allow for
eligible read/native scopes; one-shot-only operations show Review details instead
of promising a permanent grant. Activation carries independent random 256-bit
tokens for each button, bound to a live immutable daemon request. Invalid, changed,
expired, consumed or restarted-daemon activations fail closed. URI activation
forwards to the existing daemon and exits, never launches a second desktop UI.

The local dialog remains available when Windows suppresses notifications. It defaults
to Deny, uses plain text, shows command/scope/identity details, expires automatically,
and closes when a notification button resolves the request. Qt's `setDetailedText`
resets window flags: AlwaysOnTop must be set **after** constructing details (covered
by an actual offscreen Qt event-loop test).

Bounded local JSONL diagnostics record create/display/decision/close and error types;
never command bodies, activation tokens, passwords, screenshot pixels or environment values.

## Primary references (accessed 2026-09-08)

- [Microsoft: ToastNotificationManager](https://learn.microsoft.com/en-us/uwp/api/windows.ui.notifications.toastnotificationmanager)
- [Microsoft: desktop toast notifications](https://learn.microsoft.com/en-us/windows/win32/shell/quickstart-sending-desktop-toast)
- [Microsoft Windows App SDK registration implementation](https://github.com/microsoft/WindowsAppSDK/blob/main/dev/AppNotifications/AppNotificationUtility.cpp)

The implementation uses the existing Windows SDK .NET reference, not another
application's notification identity or a large additional UI runtime.
