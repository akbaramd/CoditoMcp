# Incident response

## Immediate priorities

1. Protect people/data and stop additional unsafe execution.
2. Preserve enough redacted evidence to understand scope.
3. Revoke the narrowest affected credentials, escalating to all grants/devices/keys
   when boundaries are uncertain.
4. Restore only from known-good immutable artifacts and verified data.

## Scenarios

| Signal | Immediate action |
|---|---|
| Token/grant/link exposure | Revoke grant and link, terminate affected operations, review tenant audit, reauthorize |
| Device key compromise | Revoke device and all links/tickets, close epochs, remove startup agent, re-enroll after host cleanup |
| OAuth signing key compromise | Remove key after emergency rollover, revoke affected tokens/families, publish new JWKS, reauthenticate |
| Cross-tenant routing | Disable MCP admission globally, preserve DB/audit state, revoke suspect grants, determine every affected operation |
| Sandbox escape/self-test regression | Disable isolated shell feature/release, terminate Job Objects, revoke remote shell grants if needed, patch/retest |
| Stale socket duplicate | Fence/close both connections, stop mutations, reconcile journals/hashes, mark shell uncertainty honestly |
| Database loss/corruption | Stop relay, take forensic copy, restore verified backup, rebuild Redis routing, force epoch reconnect |

Do not include tokens, full commands, file content, local roots, or sensitive output
in incident chat/tickets. Use operation/action digests and transfer sensitive evidence
through an approved encrypted channel.

## Recovery gate

Before reopening admission: root cause bounded, compromised credentials revoked,
patched image identified by digest, migrations/data verified, relevant adversarial
test added and passing, sandbox/epoch/cross-tenant smoke tests passing, and owner
records the residual risk.
