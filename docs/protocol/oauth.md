# OAuth, OIDC, and resource binding

## Public metadata

The relay publishes:

- Authorization-server metadata under the RFC 8414 well-known path.
- OIDC discovery, JWKS, authorization, token, user-info, revocation, and
  introspection endpoints.
- Protected-resource metadata for every active MCP URL. Its `resource` is exactly
  `https://codito.akbaramd.ir/mcp/d/{link_id}` and identifies the authorization
  servers/scopes accepted for that resource.

All externally generated URLs derive from the single normalized
`PUBLIC_BASE_URL=https://codito.akbaramd.ir`; forwarded host/scheme values are
trusted only from the configured proxy network.

## ChatGPT client

- Authorization code with PKCE S256 is mandatory.
- Client identity is loaded from an allowlisted HTTPS Client ID Metadata Document.
- Token endpoint authentication is `private_key_jwt`; validate issuer, subject,
  audience, signature, unique `jti`, and short assertion expiry.
- Redirect URI comparison is exact against client metadata. No wildcard, prefix,
  query stripping, localhost exception, or open redirect is allowed.
- Open dynamic client registration is disabled.
- Consent displays account, target device/link, project visibility, and requested
  scopes. A grant records the exact resource and scope set.

## Token policy

| Credential | Lifetime | Storage/rotation |
|---|---:|---|
| OAuth access token | 1 hour by default | Opaque; SHA-256 checksum only at rest; exact resource |
| Refresh token | 90-day idle window by default | Rotate every use; later reuse revokes the family |
| Authorization code | ≤ 5 minutes | One use; PKCE/redirect/client/resource bound |
| Device WS ticket | ≤ 60 seconds | One use; device key/nonce/audience bound |
| Browser session | 12h absolute / 30m idle | Secure, HttpOnly, SameSite cookie; rotate on auth |

Tokens are rejected after grant, link, device, account, or client revocation even if
their nominal expiry has not arrived. Introspection never exposes another tenant's
data. Clock-skew tolerance is small and monitored.

### Long-lived connections without permanent bearer tokens

An access-token expiry does **not** require browser login: a client exchanges its
current refresh token for a new pair at `/o/token/`. Refresh works even after the
old access token expires and after the relay web session has ended. With DOT 3.4,
the refresh idle deadline is the associated access token's expiry plus the
configured refresh lifetime; each successful rotation starts a new idle window.
This is not a promise of an uninterrupted or unrevocable login.

Operator settings, in seconds:

- `OAUTH_ACCESS_TOKEN_EXPIRE_SECONDS=3600`, allowed range 300–86400.
- `OAUTH_REFRESH_TOKEN_EXPIRE_SECONDS=7776000`, allowed range 86400–15552000
  (1–180 days).

Compose passes both values to the relay. Invalid, zero, or unlimited values fail
startup. Existing access tokens retain their stored expiry; new pairs receive
the configured access lifetime. The refresh idle setting is checked dynamically
for existing records too. No production tokens need rewriting, and deleted or
revoked tokens are never restored by this configuration change. These settings
also apply to the desktop OIDC client; browser/admin session limits stay unchanged.

`OAuthGrantFamily` preserves the same `oauth_grant_id` across rotations and old
access-token deletion. This keeps an existing locally saved screen permission
bound to the original account/client/device/link authorization, not to a
short-lived access token. Rotation cannot add a missing scope such as
`screen:read`; additional scopes require new user consent.

Clients must serialize refresh, replace the pair atomically, and discard the
previous refresh token. A request whose refresh token changed between validation
and the locked save returns JSON `invalid_grant`, never a successful empty pair;
that narrow race does not invalidate the winning rotation. A **later** replay of
the consumed token still revokes its family. DOT's built-in grace replay is
disabled because its stored-plaintext replay is incompatible with hash-only
storage. If the rotated response is lost and the client has only the consumed
token, interactive authorization may be required. Do not automatically retry a
consumed token or remove rotation/reuse protection to hide this condition.

Keep CIMD/client identity and signing configuration stable across deployments.
OpenAI distinguishes stable client credentials from access/refresh tokens, which
may expire and rotate normally; expired bearer requests must receive the proper
401 authentication challenge. [OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth)

## Desktop OIDC

The packaged desktop uses a pre-registered public client, system browser, PKCE S256,
high-entropy state/nonce, and a loopback listener bound only to an ephemeral
localhost port. It embeds no secret. The listener accepts one matching request,
validates state, closes, and exchanges the code using the exact redirect URI.

## Required scopes

- `projects:read`
- `projects:write`
- `files:read`
- `files:write`
- `shell:execute`
- `screen:read`

Scopes are additive but not sufficient: all tenant/resource/device/link/project
bindings and local device policy must also pass.
