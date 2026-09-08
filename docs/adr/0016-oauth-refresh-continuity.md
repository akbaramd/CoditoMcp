# ADR 0016 — Longer OAuth continuity with bounded rotating credentials

- Status: Accepted
- Date: 2026-09-08
- Supersedes only ADR 0002's fixed 15-minute/30-day token lifetime defaults.

## Context

The owner requested longer ChatGPT login continuity without repeated reconnects.
Browser cookie expiry, OAuth access expiry, refresh expiry, device connectivity,
and scope consent are separate states. Increasing cookie lifetime or bypassing
scope validation cannot repair an expired or revoked OAuth grant.

## Decision

Use configurable defaults of one-hour access and 90-day sliding refresh idle
lifetime for both OAuth clients. Reject access values outside five minutes–one
day and refresh values outside one–180 days. Keep web/admin sessions at 12 hours
absolute and 30 minutes idle, opaque hash-only token storage, PKCE, exact audience
binding, refresh rotation, immediate revocation, and later-reuse family revocation.

DOT 3.4 checks the refresh deadline from the associated access token's expiry,
including already-issued records. An expired access token may therefore still be
refreshed without a browser session. Newly issued access tokens receive the new
lifetime; existing access-token expiry is not rewritten.

Preserve the durable `OAuthGrantFamily.oauth_grant_id` during each rotation.
Saved display consent remains bound to the same authorization family; a new
authorization cannot silently inherit it. Refresh never broadens scopes.

Lock and recheck the refresh row inside DOT's token-save transaction. If another
rotation or revocation won after validation, return an uncached JSON
`invalid_grant`, not DOT's blank stored-token columns as a successful pair. Do
not revoke the winning pair for that already-validated concurrent loser. An
ordinary subsequent reuse still reaches DOT's family-revocation validation.

## Why no grace replay

The installed DOT 3.4 implementation's system check `oauth2_provider.E001` rejects
positive grace with hash-only storage: grace reissues the previous pair from
plaintext columns. Those columns deliberately remain blank in Codito. Enabling
plaintext storage or creating a new recoverable token cache is outside this fix.
Clients must perform single-flight refresh and persist each replacement pair.
A lost successful refresh response can still require interactive authorization;
this limitation is explicit, not disguised as guaranteed permanent login.

## Evidence and validation

- Local primary source: installed `oauth2_provider/oauth2_validators.py`,
  `models.py`, and `checks.py` from the locked DOT 3.4 dependency (read 2026-09-08).
- OpenAI permits normal access/refresh expiry and rotation while requiring stable
  registered client credentials and valid per-request authentication.
  [OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth)
  (accessed 2026-09-08).
- Automated tests cover real PKCE issuance, browserless refresh after 31 days of
  access expiry, multiple rotations with the same screen grant, exact 90-day idle
  expiry, immediate revocation, later-reuse family revocation, hash-only storage,
  invalid configuration, and a deterministic validation/save interleave.
- The PostgreSQL-only two-thread test synchronizes validation before competing
  token-save transactions, checks one valid winner and one JSON rejection, and
  verifies that the winner can rotate again. SQLite skips that row-lock proof;
  PostgreSQL relay CI runs it.

These source/tests are not evidence that a particular production rollout or real
ChatGPT reconnect has succeeded. No production token or user records are edited.
