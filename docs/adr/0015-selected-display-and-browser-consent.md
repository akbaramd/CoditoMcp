# ADR 0015 — Selected-display consent and bounded browser opening

Accepted 2026-09-08 following the owner's explicit request for selected-monitor
screenshots, locally saved Always allow, and opening a requested URL in Firefox.
Supersedes ADR 0014 only for the primary-display-only and one-shot-only restrictions.
Its OAuth, secure-desktop, image-size, and retention boundaries remain applicable.
This decision is not evidence of a published release or successful live ChatGPT test.

## Screenshot selection and consent

`device_screenshot` still requires OAuth `screen:read`. It accepts two actions:

- `list_displays`: returns up to 16 display descriptors, opaque IDs, primary flag,
  dimensions, scale, topology fingerprint, and whether saved permission is supported.
  This is metadata only; it does not capture pixels or ask for pixel access.
- `capture`: resolves `primary` or an opaque ID returned by `list_displays` to one
  current display before requesting consent. The captured PNG identifies that display.

The interactive companion resolves the Windows monitor interface and its serial
or validated EDID configuration. The display identity is combined with the full
display topology, including layout, dimensions, scale and primary assignment.
Ambiguous identities are rejected. A missing stable interface/configuration allows
one-shot capture only, not Always allow.

This is consent to a **logical display configuration**, not forensic identification
of physical hardware. Identical serial-less monitors replaced at the same port
with identical EDID/layout can have the same identity. Do not promise detection of
that replacement. Other displays and changed topology require fresh approval.

Each capture asks Deny or Allow unless the local user explicitly saves Always allow
for that selected display. A saved permission binds account, OAuth grant, link,
device, `screen:read`, display ID, display identity and topology. It survives normal
reconnect/restart, but never broadens to all monitors, another grant, file access,
shell execution, browser opening, or secure-desktop capture. The dialog states that
future captures can include private visible windows without another prompt.

Settings lists saved screenshot permissions and offers **Revoke all screenshot
permissions**. Sign-out/re-enrollment removes saved permissions. Revocation and
security-generation changes invalidate pending work. Lock/sleep still prevent
capture; a saved grant is not permission to bypass the locked Windows desktop.

The daemon verifies approval generation/deadline before capture and after receiving
the result. The tray rechecks current topology and unlocked Default desktop before
grabbing one screen. Queued UI requests are claimed once, so lost IPC responses
cannot repeatedly capture. No fallback to another monitor is permitted.

## Notification behavior

An arriving approval displays a notification without automatically raising the
Codito window or a modal dialog. Native notifications have Deny, Allow and, when
eligible, Always allow. One-shot-only actions offer Review details instead of
promising a persistent grant. The local user can open details from the notification
or **Review pending approvals**. Unanswered/expired requests never imply consent.

Native foreground toast activation uses Codito's registered per-user COM activator.
The `codito-approval` value is activation data forwarded to the installed tray and
authenticated daemon; normal toast buttons do not ask Windows to launch that URL
scheme. One-use random tokens bind each button to the immutable pending request.
Forged, changed, expired or consumed tokens fail closed. No system notification
preference or another application's association is overridden.

## Bounded browser action

The seventh MCP tool is `device_desktop` with only `action=open_browser`. Inputs are
`browser=default|firefox`, an absolute HTTP/HTTPS URL, and a bounded purpose. OAuth
`shell:execute` permits requesting this capability; **separate one-shot Windows
approval** for `desktop:open_url` is always required. Existing file/shell/screenshot
Always allow permissions cannot authorize it.

The prompt names the exact browser and normalized URL and warns that the browser
contacts the destination using its existing profile and website sessions. Inputs
reject credentials, control characters, malformed escaping, file/custom schemes,
arbitrary executable paths and command-line flags. The default browser opens through
Qt's URL API. Firefox uses only its fixed registered Windows App Paths executable,
with a fixed new-tab argument and sanitized standard-user environment; no shell or
PATH lookup is involved. Missing Firefox does not silently open a different browser.

Success means only `status=submitted`: Windows accepted a browser-open request.
It does not prove navigation, page load, login or rendering completed. A screenshot
is a separate subsequent tool call using the selected monitor and its own consent.
No arbitrary click/type/scroll, DOM access, tab discovery, hidden browser profile,
credential entry, elevation or general remote GUI control is added.

Browser requests are claimed at most once. If a request might have reached the
desktop but its result is lost, report non-retryable `outcome_unknown`; never
automatically replay it after socket loss or process restart. No browser command
is queued for an offline device.

## Image delivery, retention and acceptance

Capture remains limited to PNG, 640–2048 maximum dimension, at most 600 KB of image
bytes and 800 KB base64. The relay returns native MCP ImageContent plus metadata;
SQLite/PostgreSQL durable result journals omit pixels. Redis transport/result
streams may hold pixels for the existing five-minute TTL, and persistence/backups
and ChatGPT have their own retention. This is not zero-retention screen sharing.

Unit/integration tests use mock browsers and synthetic pixels. Release acceptance
must separately verify installed Windows toast actions, local permission revocation,
selected-monitor behavior, browser submission and an actual ChatGPT image. A test
suite passing or the server listing a tool is not proof of that end-to-end outcome.
