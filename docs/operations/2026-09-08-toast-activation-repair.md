# Windows approval activation repair — 2026-09-08

## Observed failure

The v0.1.9 native notification displayed its actions, but Windows opened a
Microsoft Store handler chooser for `codito-approval` instead of delivering the
approval to Codito. This is a local activation failure, separate from OAuth or
screen capture permission.

Read-only checks on this Windows 11 host showed that the per-user protocol
command existed, its target executable existed, and
`AssocQueryStringW(ASSOCF_IS_PROTOCOL, ASSOCSTR_COMMAND)` resolved the expected
v0.1.9 tray. However, `Launcher.QueryUriSupportAsync` returned `NotSupported` for
Codito while HTTPS, VS Code and Codex returned `Available`. Completing the
Capabilities/RegisteredApplications/ProgID metadata and notifying the Shell did
not change that result. Registry existence and toast display success therefore
were not sufficient acceptance tests. The deeper Windows association failure
has not been attributed to a specific Windows policy or cache.

## Implemented route

New notifications use Windows' supported unpackaged desktop COM activation:

1. Startup and the installer register only Codito-owned HKCU keys: the fixed
   notification CLSID, its `LocalServer32` command, and the AUMID's
   `CustomActivator`. Legacy URL registration remains for compatibility. No
   `UserChoice`, OS notification preference, elevation or machine-wide key is
   modified.
2. The tray starts one hidden broker `--toast-listener` and requires its `ready`
   handshake before displaying a notification. The broker registers a live COM
   class factory. It exits when the tray's stdin pipe closes, including tray
   crash/exit. The standalone `--toast-server` fallback has a 30-second bound.
3. Toast XML uses `activationType="foreground"`. The sealed URI is now opaque
   callback argument data, **not** a URI Windows needs to launch. Foreground
   activation does not mean opening the Codito window.
4. Windows calls `INotificationActivationCallback`; the broker checks the Codito
   AUMID and argument bounds, then starts only its fixed sibling tray executable
   using `ProcessStartInfo.ArgumentList`, no shell. The tray forwards to the
   daemon's authenticated IPC and exits without starting another dashboard.
5. The daemon retains one-use, request/decision-bound random tokens and expiry
   checks. COM activation alone never grants permission. Screen `Always allow`
   is a distinct decision from file and shell permissions.
6. Registration removes only Codito's `approvals` notification history so old
   protocol-based buttons do not survive an update. Pending daemon requests are
   re-presented by the restarted UI.

The live COM class is deliberate: an isolated probe of automatic COM launch
through per-user registry returned `REGDB_E_CLASSNOTREG` on this host, while the
already-running registered listener handled real cross-process COM calls. The
normal user identity was checked; neither probe ran as an administrator.

## Verification

- Broker build: zero warnings/errors. Seven .NET tests, including COM interface
  exposure, rejection of aggregation, foreign-AUMID/bad-argument rejection.
- Actual Windows probe: started the hidden listener, called the real COM class
  from another process, and received `E_INVALIDARG` for a deliberately invalid
  foreign app ID. No real request ID/token was used, no approval was granted,
  no tray was opened and no screen was captured.
- The broker is now a Windows-subsystem executable to avoid a console stealing
  focus. Its redirected JSON `probe` still returned exit 0 and valid output.
- The listener readiness handshake, singleton behavior and stdin-owned shutdown
  are covered by Python tests; `Test-ToastActivation.ps1` reproduces the harmless
  callback check while a listener is running.
- Real notification-button approval and image delivery still require the user
  to click a newly issued notification after installing this build. Tests do not
  imply that a real ChatGPT screenshot was accepted.

## Primary references (accessed 2026-09-08)

- [Microsoft desktop toast activation options](https://learn.microsoft.com/en-us/windows/apps/develop/notifications/app-notifications/toast-desktop-apps)
  documents COM as the supported foreground/background path for unpackaged apps.
- [Microsoft/Community Toolkit COM registration implementation](https://github.com/CommunityToolkit/WindowsCommunityToolkit/blob/main/Microsoft.Toolkit.Uwp.Notifications/Toasts/Compat/ToastNotificationManagerCompat.cs)
  supplies the AUMID `CustomActivator`, per-user `LocalServer32`, and class-factory
  reference behavior; Codito uses the existing .NET runtime and adds no UI SDK.
- [SHChangeNotify](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shchangenotify)
  and [Default Programs registration](https://learn.microsoft.com/en-us/windows/win32/shell/default-programs)
  explain publishing protocol associations without replacing user choices.
- [QueryUriSupportAsync](https://learn.microsoft.com/en-us/uwp/api/windows.system.launcher.queryurisupportasync)
  is the non-launching Windows handler support check used during diagnosis.
