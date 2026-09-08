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
| MCP access token | 15 minutes | Opaque; only a keyed hash at rest; exact resource |
| Refresh token | 30 days | Rotate every use; family revoked on reuse |
| Authorization code | ≤ 5 minutes | One use; PKCE/redirect/client/resource bound |
| Device WS ticket | ≤ 60 seconds | One use; device key/nonce/audience bound |
| Browser session | 12h absolute / 30m idle | Secure, HttpOnly, SameSite cookie; rotate on auth |

Tokens are rejected after grant, link, device, account, or client revocation even if
their nominal expiry has not arrived. Introspection never exposes another tenant's
data. Clock-skew tolerance is small and monitored.

## Desktop OIDC

The packaged desktop uses a pre-registered public client, system browser, PKCE S256,
high-entropy state/nonce, and a loopback listener bound only to an ephemeral
localhost port. It embeds no secret. The listener accepts one matching request,
validates state, closes, and exchanges the code using the exact redirect URI.

## Required scopes

- `projects:read`
- `files:read`
- `files:write`
- `shell:execute`

Scopes are additive but not sufficient: all tenant/resource/device/link/project
bindings and local device policy must also pass.
