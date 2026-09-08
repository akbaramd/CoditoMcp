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

## Published and installed evidence

- GitHub v0.1.10 was published successfully by release run `34233228721`;
  CI run `34233228736` passed all five jobs (including dependency/secret scans).
- Release commit: `4afbeca062ec06b829d3d354c22b511de67b5db4`.
- ZIP: 140,369,834 bytes; SHA-256
  `20ec557349196d4a6b6bee29d3d18ae93dcd05eb63d96b0d7921c16150e1ae52`.
- Installed daemon/tray paths both point to the per-user `app/0.1.10` package.
- Relay image: `docker.wa-nezam.org/codito/relay:0.1.10-4afbeca062ec`, digest
  `sha256:ca2dce0700ed47561164118c57e5bf2d9fde35d592cb9efeccfe46d8ac07ce00`.
  Migration `core.0011_desktop_actions` applied; existing PostgreSQL/Redis reused.
- Pre-migration backup: `/data/backups/codito/daily/codito-20260908T133945Z.dump`.
  Previous image configuration preserved in `.env.pre-0.1.10` on the server.
- Public readiness returned version 0.1.10 with database/Redis/OIDC signing checks OK.
  Installed daemon reported online at connection epoch 134.
- Installed COM listener logged ready; real cross-process foreign-app probe passed.
  Test notification logged `toast_shown` and `approval_displayed`, but the user did
  not observe the first banner. A new display-only diagnostic was submitted on request.
  **No successful real button click or ChatGPT screen rendering is claimed.**
- The conversation's existing Codito plugin still exposes only its four older tools,
  even after a fresh mention. `device_screenshot` and `device_desktop` are absent from
  this session's callable tool catalog. Runtime tool refresh/user consent is needed
  to test those features here; shell is not used to bypass `screen:read`.
- A real MCP read through the deployed relay/WSS/device returned version 0.1.10
  from project `pyproject.toml`; operation `003bc011-a173-4895-ad4b-e1f58b56ef70`.
