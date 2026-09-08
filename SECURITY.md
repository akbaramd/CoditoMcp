# Security policy

Codito is security-sensitive remote execution software. During the MVP, report a
suspected vulnerability privately to the repository owner; do not open a public
issue with tokens, device identifiers, commands, filesystem details, or exploit
steps.

## Supported version

Only the most recent immutable release tag deployed by the owner is supported.
Development builds and unsigned Windows installers are not production supported.

## Minimum report

Include the affected version, trust boundary, reproducible steps using synthetic
data, expected/actual behavior, and whether credentials or cross-tenant access may
be involved. Revoke affected links and devices immediately through the relay UI.

## Secret handling

Never commit `.env`, database dumps, private keys, OAuth tokens, enrollment links,
invitation URLs, password-reset URLs, approval signatures, or production logs.
