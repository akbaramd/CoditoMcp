# Threat model

- Review date: 2026-09-09
- Method: trust-boundary/abuse-case analysis informed by STRIDE
- Protected assets: accounts, OAuth grants/tokens, device identity, local files,
  command authority, approval intent, operation integrity, audit history, service
  availability, managed-browser profiles and local web-session state

This document specifies required controls. A control is not considered implemented
until its checklist test passes on the release artifact.

## Actors and assumptions

| Actor | Trust |
|---|---|
| Account owner at local Windows session | May authorize local/native actions; can still make mistakes |
| ChatGPT/MCP client with valid grant | Authorized only to exact resource/scopes; tool content is untrusted input |
| Relay operator | Controls service/DB host but should not receive absolute paths or device private keys |
| Other Codito account/device | Fully hostile across tenant boundary |
| Project content/dependencies | Hostile; filenames, patches, build scripts, and output may attack parsers/UI |
| Network/edge | Hostile outside TLS; configured proxy can observe plaintext relay traffic |
| Local process in same user session | Potentially hostile; named-pipe and approval spoofing are in scope |
| Project dev server and rendered page | Hostile; can emit deceptive DOM/source metadata, redirect, open windows, download, or flood events |

The MVP does not defend against a fully compromised Windows account, relay root,
malicious trusted code-signing key, hardware/firmware compromise, or attacks after a
user explicitly enables `native_trusted` and runs hostile code.

## Trust boundaries

1. ChatGPT ↔ public TLS/OAuth/MCP edge.
2. Edge proxy ↔ relay ASGI network.
3. Relay worker ↔ PostgreSQL/Redis.
4. Relay ↔ outbound device WebSocket.
5. Agent daemon ↔ UI named pipe.
6. Agent ↔ .NET broker.
7. Broker/AppContainer ↔ registered project and Windows host.
8. Managed Chromium ↔ loopback project server and external resources loaded by that page.

## Abuse cases and required controls

| ID | Threat | Impact | Required prevention/detection |
|---|---|---|---|
| T01 | Guess/share device link | Unauthorized project discovery | 128-bit route ID is non-secret; OAuth always required; grant/link/device binding; revocation |
| T02 | Token audience confusion | Use token at another device link | Exact RFC 8707 resource at issuance and request; RFC 9728 path metadata; fail closed |
| T03 | Cross-account/project substitution | Operate on foreign project/job | Tenant-keyed query from auth context; composite constraints; non-oracular errors; 2×2 matrix tests |
| T04 | OAuth redirect/client forgery | Account takeover/token theft | Exact redirect allowlist, PKCE S256, CIMD allowlist, private_key_jwt, state/nonce, no open DCR |
| T05 | Refresh replay | Long-lived account access | One-time rotation, atomic family chain, family revocation/re-auth on reuse |
| T06 | Invitation/reset theft or replay | Account takeover | Random selector, stored hash, short expiry, single atomic use, redacted logs; manual-delivery limitation disclosed |
| T07 | Stale/split-brain socket | Duplicate or misrouted operations | Atomic monotonic connection epoch; epoch compare on every state transition; close previous claim |
| T08 | Dispatch crash boundary | Duplicate/lost mutation | Relay persist-before-publish; device persist-before-receipt; patch idempotency/hashes; shell `outcome_unknown` |
| T09 | Path traversal/alias | Read/write outside root | Reject absolute/drive-relative/UNC/device/ADS/dot components/reserved aliases; handle walk |
| T10 | Junction/symlink race | Escape after validation | Open components without reparse traversal; root/final identity recheck; operate via validated handles |
| T11 | Hardlink mutation | Modify file outside root | Reject mutation when link count >1 in v1; recheck immediately before replacement |
| T12 | Cloud/removable/root registration | Unstable or overbroad access | Fixed local NTFS/ReFS only; reject drive root, reparse root, placeholders, remote/removable media |
| T13 | Patch ambiguity/partial commit | Corruption or wrong edit | Base hashes for all paths; exact unique hunks; all-file preflight; journaled atomic replacement/recovery |
| T14 | Shell metacharacter/PATH hijack | Unexpected code execution | Structured argv preferred; explicit script kind; sanitized PATH/env; display resolved executable; no shell interpolation for argv |
| T15 | Child breakaway | Uncontained process | AppContainer token + Job Object kill-on-close/no breakaway; restricted handle inheritance; adversarial child tests |
| T16 | Network exfiltration | Leak secrets/source | No AppContainer network capability; verify denial; protected locations hard denied; output cap |
| T17 | Sandbox downgrade | Native execution without intent | Broker/self-test fail closed; mode locally signed/stored; never accept mode/approval from model or relay |
| T18 | Approval spoof/replay | Unintended native/destructive action | Authenticated pipe, signed request/decision, exact digest/bindings/epoch/expiry; toast cannot approve |
| T19 | Overbroad session approval | Persistent command authority | Only eligible isolated/read-only capabilities; 30m absolute/10m idle; clear on security/lifecycle events |
| T20 | Secret exposure in UI/logs/telemetry | Credential/data leak | Central structured redaction, no bodies/output/env/absolute path/tokens, synthetic fixtures, log tests |
| T21 | Output/frame exhaustion | Agent/relay denial of service | Frame/input/output/queue/time limits, streaming backpressure, terminate on cap, per-account/device rate limits |
| T22 | CSRF/session fixation | Browser account changes | Django CSRF, Secure/HttpOnly/SameSite cookies, session rotation, idle/absolute expiration |
| T23 | SSRF through CIMD/JWKS | Relay network access | Explicit HTTPS host/client allowlist, DNS/IP policy, size/time limits, cached pinned metadata, no redirects to private space |
| T24 | Redis/PostgreSQL interruption | Inconsistent state | DB conditional transitions, readiness failure, pause admission/routing, reconciliation and restart matrix |
| T25 | Malicious update/dependency | Supply-chain compromise | Locked hashes, reviews, Dependabot/audit, SBOM, Trivy, secret scan, immutable signed release hooks |
| T26 | Backup loss/disclosure | Permanent loss or credential leak | Restricted permissions, encrypted/off-host post-MVP, restore drill, exclude ephemeral plaintext secrets |
| T27 | Frontend session substitution/stale element | Act on another project or changed DOM node | Bind session to account/grant/link/device/project/root/security generation; require snapshot ID; invalidate registry on action/navigation/HMR |
| T28 | Malicious source metadata | Read or edit outside project; false source certainty | Treat DOM attributes as untrusted; project-relative parsing; resolver containment and file/coordinate verification; explicit exact/heuristic/unavailable |
| T29 | Browser escape/navigation abuse | General remote browser or desktop control | Agent-owned profile/executable; Chromium sandbox required and package-smoke tested; loopback configured origin only; relative start route; block popups/downloads/external top-level navigation; no raw selectors/JS/CDP/uploads/clipboard/permissions |
| T30 | Credential capture through fill/profile reuse | Account compromise | Never reuse personal browser profiles; reject password and credential-like fills; visible browser for local-user login; never return cookies/storage/headers/bodies |
| T31 | Console/network/image exfiltration or frame exhaustion | Secret exposure or denial of service | Redact and cap console entries; strip network query/headers/body; cap tree/image/frame; omit pixels from durable journal |
| T32 | Orphaned dev server/browser | Persistent native process or resource exhaustion | Broker-owned process tree; one session per project; reference ownership; TTL/security-generation/shutdown cleanup; never kill a reused server |

## Hard denials

These never become generic session grants in v1: elevation; OS/device namespaces;
external mutation; secret/credential locations; deletion; reparse or hardlink
mutation; arbitrary interactive/PTY/GUI/detached execution. ADR 0020 adds only an
agent-owned Chromium and an owned project dev-server tree behind separate frontend
and shell scopes; it does not grant general GUI or process control. Any broader
exception needs a new ADR and explicit local UX.

## Security test corpus

Release tests must include:

- `..`, mixed separators, repeated separators, Unicode lookalikes/normalization,
  drive-relative paths, UNC, `\\?\`, `\\.\`, ADS, reserved names, trailing dot/space.
- Junction/symlink root and component swaps at every validate/open/replace boundary;
  hardlinks created before and during mutation; case and file-ID replacement.
- Shell quoting/metacharacters, poisoned PATH/PATHEXT/COMSPEC, inherited handles,
  breakaway grandchildren, timeout/cancel races, GUI/elevation attempts, and network
  connections over IPv4/IPv6/DNS/loopback.
- Approval request/decision replay, wrong epoch/project/grant/digest, expiry, forged
  toast/pipe client, lock/sleep/logout/restart/disconnect invalidation.
- Expired/revoked/wrong-audience tokens, redirect URI variants, PKCE downgrade,
  malicious CIMD/JWKS, stale sockets, sequence replay/gaps, frame bombs.
- Two accounts × two devices × duplicate project titles × jobs/patch keys to prove
  no cross-routing.
- Relay worker, Redis, PostgreSQL, agent, and network restart at each operation
  transition.
- Frontend route/origin tricks, popup/download/external-navigation attempts,
  sensitive-input fills, forged/traversing source attributes, stale element IDs,
  HMR invalidation, event/image/frame floods, reused-server cleanup, and profile isolation.

## Residual risk

- `native_trusted` is intentionally equivalent to remote code execution as the
  logged-in user for that project context.
- The user-managed TLS proxy and relay host are privileged infrastructure.
- Same-host backups cannot survive host/disk loss.
- Local malware running as the same user may attack UI or files despite IPC
  authentication; OS account integrity remains an assumption.
- Novel Windows sandbox escapes require patching and possibly device/link revocation.
- An exact frontend source result proves that development instrumentation named a
  current in-project coordinate; it cannot prove hostile page code never copied a
  valid attribute between nodes. Hash-verified reads/patches and human/code context
  remain required before mutation.
