# Native notification and selected-screen repair

## Observed cause

The installed 0.1.9 toast used `activationType=protocol`. Its action URI used
`codito-approval`. Windows could resolve the legacy Shell association, but WinRT
reported that URI unsupported. Clicking Allow or Always allow therefore opened
the Microsoft Store association prompt, not Codito's approval handler. Registry
presence and a successful toast display were insufficient acceptance tests.

## Implemented correction

- Foreground toast activation uses a fixed COM callback. URI-shaped text is
  callback argument data only, never a protocol navigation. The callback launches
  only the installed sibling tray with an argument list, not a shell.
- A hidden COM listener is owned by the tray's stdin pipe. It must acknowledge
  readiness before a toast is sent; it exits when the tray exits. This also avoids
  dependence on per-user COM process activation, which failed on this host even
  though the live registered COM callback succeeded.
- Per-button one-use daemon tokens remain required. COM activation does not itself
  grant access. Foreign application IDs, stale tokens and substituted requests fail.
- Installer registration removes only Codito's old `approvals` notification group.
  Old protocol-activation payloads must not remain actionable after update.
- Arriving requests no longer raise Codito or a modal. Only an explicit Review
  action opens the exact pending request. Manual screen/browser approval minimizes
  the review window before dispatch.
- Screenshot Always allow is separately scoped to the selected logical display
  configuration and requesting account/grant/link/device, with Settings revocation.
- `device_desktop` opens a requested HTTP(S) URL in default browser or Firefox
  after separate one-shot consent. Screenshot is a separate tool request.

## Local evidence

On 2026-09-08, the real Windows out-of-process COM diagnostic created the callback
successfully and rejected a harmless foreign-app activation with `E_INVALIDARG`.
No live approval token, screenshot or browser action was used in that probe.
The WinExe broker still returns redirected JSON for its standard capability probe.

Before final notification tests, 401 Python tests passed; protocol coverage was
98.75%, with strict mypy passing 80 source files. Later focused tests additionally
cover malformed browser terminal results, actual MCP image serialization and
COM listener startup/cleanup. Final release evidence is recorded after deployment.

## User-assisted acceptance (still required)

1. Confirm installed desktop and public relay show the new matching version.
2. Refresh ChatGPT tools/consent if the existing grant lacks `screen:read`.
   See [scope diagnosis](2026-09-08-screen-oauth-scope.md).
3. Call `device_screenshot(action="list_displays")`; select one returned ID.
4. Call capture for that ID. Click **Always allow** in the new Windows toast.
   Verify no Store dialog and no Codito foreground window, and that ChatGPT renders PNG.
5. Capture that display again: no second approval. Revoke in Settings and confirm
   the next capture requires consent. Different monitor/layout must ask again.
6. Ask `device_desktop` to open Firefox at an explicit URL; approve once. Then
   request a screenshot. Opening a URL reports submission, not completed page load.

Do not run the COM diagnostic with a real approval URI. Do not export raw tokens,
URLs with user data, or private screenshot pixels to logs or the repository.
