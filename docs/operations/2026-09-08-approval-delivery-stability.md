# Approval delivery and connection stability follow-up

## Evidence, not assumptions

The user reported that no approval notification was visible. The old
`toast_shown` / `approval_displayed` events recorded `ToastNotifier.Show` acceptance,
not proof of banner presentation. A pending queue item does not mean the user
ignored a visible prompt.

Read-only checks on this Windows installation found notifications enabled,
`SuppressPopup=false`, a Codito entry in native notification history, and Windows
PushNotification-Platform event 3153 confirming platform delivery. The Shell query
returned `Busy`. That state can indicate a fullscreen application or presentation
conditions; it suggests suppression but does not establish the exact reason the
banner was not visible. No Windows notification, domain clock, or security setting
was changed.

Reference: [Microsoft SHQueryUserNotificationState values](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ne-shellapi-query_user_notification_state),
accessed 2026-09-08.

## Queue defect

An unanswered diagnostic request remained at the head of the tray's peek queue.
The tray remembered having notified that request, then repeatedly peeked it,
preventing subsequent screenshot requests from receiving their own notification.

The background peek now excludes already-notified IDs, with a bounded 32-item
list. The daemon returns pending IDs for pruning; an empty peek no longer clears
all delivery state and produces a notification storm. Explicit review still
retrieves its exact request, independently of this background exclusion.

The actual screenshot operation `c7428373-e6a4-44fc-94d7-4ff8e56dd983` reached the
relay and agent and eventually failed with `approval_expired`. The associated MCP
HTTP request returned 200 after its wait deadline. This is not evidence of an
HTTP 404. The separately reported `Resource not found` remains uncorrelated with
a raw client request; there is no separate screenshot HTTP endpoint. Screenshot
uses `tools/call` at the same device MCP URL as the other tools.

## Delivery contract

- Native submission reports `delivery=submitted` and `banner_visible=null`.
- Only a rendered Codito approval surface marks local UI delivery. Rendering is
  not user consent; only a valid one-use decision resolves an approval.
- Diagnostic output contains bounded status enums and own notification tags,
  never toast XML, activation tokens, command bodies or screenshot pixels.
- Native callbacks retain the fixed COM activator introduced in 0.1.10. They do
  not invoke a custom URI protocol or open a Store association dialog.
- The clearly branded, app-owned corner panel is a fallback approval surface,
  not an imitation Windows notification and not the main Codito dashboard.
  It does not change Windows suppression settings or claim to override them.
- The panel must not steal focus, appear on a locked/secure desktop, resolve a
  request on arrival, or offer stale/unsupported permission decisions.
- On a local decision, hide approval panels and withdraw only the corresponding
  Codito native notification before forwarding the one-use decision token.
- Always allow remains capability/scope/account/grant/link/device-bound. Screen
  permission cannot silently become shell, browser, or file permission.

## Clock observations

One simultaneous read showed the Windows wall clock about 58 seconds behind the
relay. Windows used the organizational domain time source; the relay reported
NTP synchronized. No clock configuration was modified. Wire operation budgets
must not be lengthened merely because the receiving wall clock is behind.

The agent now caps the first local execution window by the wire duration and
uses an event-loop monotonic deadline. Receipt/dispatch waiting consumes that
same budget. Reconnects retain the first budget; crash reconciliation uses the
original local journal receipt rather than allocating a fresh execution window.
The original envelope, bindings, digest, and wire journal deadline are unchanged.
Unknown one-way latency and wall-clock adjustments across process restarts cannot
be exactly reconstructed; the calculation intentionally shortens retries where
evidence is uncertain. Native shell/browser timeouts after handler entry remain
non-retryable `outcome_unknown`, not permission to automatically launch again.

## Login continuity

See [ADR 0016](../adr/0016-oauth-refresh-continuity.md). Access tokens default to
one hour and rotating refresh credentials to a 90-day idle window. Browser/admin
session limits remain separate. Ordinary refresh retains the OAuth grant identity
used by saved screen consent. A row-lock recheck prevents concurrent rotation from
returning an empty token pair under hashed-at-rest storage. Revocation, expired
refresh credentials, and later reuse still fail closed. Lost refresh responses
are not silently replayed; infinite login continuity is not claimed.

The independent UI review also identified quitting while notification QThreads
were running. Shutdown must stop polling, hide pending surfaces and drain tracked
notification workers before destroying Qt objects, without forwarding new choices.

## Acceptance boundaries

Automated synthetic tests do not authorize real screen capture and do not prove
the user saw a native banner. Release/install evidence and user-confirmed button
activation are recorded separately after verification. Never declare uninterrupted
connectivity: power loss, sleep, network loss, explicit revocation, and host-side
plugin availability remain outside a promise of continuous service.

## Release and real acceptance evidence

- Feature commit: `258c40e209abd0bf9370194883853260b606e9a1`.
- Release: [v0.1.11](https://github.com/akbaramd/CoditoMcp/releases/tag/v0.1.11),
  commit `9dd6f366e05e9155ef3d4ba455a3fd5e5ecd8d96`.
- [CI 34238266054](https://github.com/akbaramd/CoditoMcp/actions/runs/34238266054)
  passed all five jobs, including the PostgreSQL-backed relay tests. Release run
  `34238266044` also succeeded.
- Final local suite: 471 passed, one PostgreSQL-only case skipped locally and
  covered by CI. Strict mypy passed 80 modules; broker tests: 15 passed.
  Staged secret scan found no leaks. One upstream Starlette deprecation warning
  remains; it is not a failing test or connectivity error.
- ZIP: 140,396,973 bytes; SHA-256
  `dca181b312dca3074e4f8e902b0e39854436861148e9f0b4504959176c96ef2e`.
  Installer verified the release digest and started daemon/tray from `app/0.1.11`.
- Relay image: `docker.wa-nezam.org/codito/relay:0.1.11-9dd6f366e05e`,
  digest `sha256:84c05d60522a4f097196c4300959b2645c3d4fa5fc6fa69719771d88c94447a7`.
  Cached base images and existing shared PostgreSQL/Redis were reused.
- Database backup: `/data/backups/codito/daily/codito-20260908T143336Z.dump`.
  Rollback configuration: `.env.pre-0.1.11` and `compose.yaml.pre-0.1.11` in the
  existing deployment directory. No new migrations were required.
- Public readiness reports version 0.1.11; database, Redis and signing-key checks
  pass. Runtime OAuth settings verified: access 3600 seconds, refresh 7776000
  seconds. Device reconnected automatically at epoch 136 and was online.
- A real MCP file read returned project version 0.1.11, operation
  `e4070824-c24f-4da8-90fa-69e8e59ceb70`.
- Harmless diagnostic approval `approval_pPq6HQC84XtjBycHc5Po6-jUuszP2NOF`
  produced `toast_submitted`, then `approval_corner_displayed`, then the user's
  `allow_once` decision. The user confirmed seeing the notification. No real
  capability was granted by this diagnostic.
- The installed COM probe initially ran before the lazy listener was started and
  reported class-not-registered. After the first toast started its owned listener,
  the same out-of-process probe passed and rejected the foreign-app input. This
  startup probe is not itself an approval or evidence of banner visibility.
- Real screenshot approval `approval_lUXzLo6kt5S_vbcu3cN0Rg2NXPl4Lmce` recorded the
  user's `allow_always_screen`, followed by `screen_captured`. Relay operation
  `bbd0a568-fd67-4a51-99e5-40264d495da4` succeeded with no error. The user explicitly
  confirmed that the resulting image appeared in ChatGPT.
- A subsequent screenshot operation `df94a110-c766-43e8-ab94-cb197bcb5ceb` also
  succeeded. Neither private pixels nor approval credentials were copied into
  these records, the repository, or diagnostic output.

Screenshot journals intentionally retain a non-replay placeholder rather than
image bytes. The current local Activity view may show that placeholder's
`outcome_unknown` next to a succeeded state; it is not the live capture result.
The relay's authoritative status for both accepted captures above is `succeeded`
with an empty error code. A clearer historical label is a follow-up UI improvement.
