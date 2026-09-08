# ChatGPT OAuth connection failure — 2026-09-08

Status: the user's real ChatGPT retry confirmed the authorization defect.
Secret-safe diagnostics are deployed. The authorized OAuth fix is implemented
and regression-tested; production acceptance must be checked after deployment.

## Implemented correction

- Approved ChatGPT CIMD clients can request the advertised `openid` and `profile`
  identity scopes alongside MCP tool scopes. `device:manage` remains desktop-only.
- Existing and newly registered CIMD clients use the relay's configured RS256
  key for ID tokens and ID-token-hint verification. DOT leaves their database
  algorithm field blank, so the validator supplies this policy request-locally;
  no client IDs, secrets, redirect registrations or database rows are replaced.
- Client-specific defaults replace DOT's global all-scopes default. MCP requests
  omitting scope get only `projects:read files:read`, not identity or mutation
  authority. Explicit `openid` is required for OIDC nonce handling.
- Discovery advertises only the implemented RS256 ID-token algorithm. The MCP
  bearer challenge advertises the MCP tool scopes, not desktop enrollment scope.
- Tests submit hidden fields parsed from the actual consent HTML. Both public
  PKCE and signed `private_key_jwt` exchanges verify the ID token using published
  JWKS (issuer, audience, subject, expiry and nonce), check UserInfo, and retain
  exact device-resource bindings. Refresh, ID-token hints, omitted scopes and
  rejection of desktop privileges remain covered. All 75 relay tests pass.

This fixes the captured authorization rejection; the real client must still
exercise its own assertion and callback after deployment. Never label a synthetic
token or a local OAuth test as a successful real ChatGPT connection.

## Confirmed live retry, 10:42:37 UTC

After deployment of `0.1.4-8d9cca8eecec`, the user repeated Connect and reported
failure. The actual request has correlation ID
`f6ad441ffcd641f39794b93cbc43c763`:

1. `http.start`: `oauth_authorize`, GET.
2. `oauth.cimd_fetched`: metadata retrieval succeeded.
3. `oauth.scopes_rejected`: reason `client_policy`; requested scopes were
   `files:read files:write openid projects:read projects:write shell:execute`.
4. `oauth.outcome`: HTTP 302, `error=invalid_scope`,
   `authorization_code_issued=false`. The request included one resource and a
   PKCE challenge. There were no unknown scopes.

This confirms rejection of the advertised `openid` scope at authorization, before
code issuance or token exchange. It is no longer only a simulated reproduction.
It does not establish that every downstream step will succeed once this is fixed.

Deployment evidence:

- Image: `docker.wa-nezam.org/codito/relay:0.1.4-8d9cca8eecec`.
- Digest: `sha256:f7334e172d142358c17c72e6d4630275b6213c608b5b43066d7f3d347b3ad2df`.
- Backup: `/data/backups/codito/daily/codito-20260908T104109Z.dump`.
- Public readiness and correlation headers checked; Docker log rotation verified
  at five 10 MB files. PostgreSQL and Redis were reused, not upgraded or pulled.
- 74 relay tests, 133 protocol/agent tests, Ruff and strict mypy passed locally;
  GitHub CI run `34216649596` succeeded.
- Synthetic probes at 10:41:51 UTC intentionally produced `invalid_grant` and
  unauthenticated MCP 401. Those probes must not be confused with the user's
  `invalid_scope` event at 10:42:37 UTC.

## Initial production observations (before diagnostic deployment)

- Running relay image: `docker.wa-nezam.org/codito/relay:0.1.4-97f71b4118cc`.
- Public `/health/ready` returns `ready`, version `0.1.4`; database, Redis,
  and OIDC signing key checks pass.
- The device associated with the newly supplied MCP link is online, and that
  link is not revoked. The earlier revoked link is not this incident's explanation.
- Recent relay logs do not contain OAuth request-level outcome diagnostics.
  No successful ChatGPT access token or current authorization-code grant was
  present when inspected. This does not by itself distinguish an authorization
  rejection from a later exchange failure.

## Reproduced defect

Both public discovery documents advertise `openid` and `profile` from the global
scope configuration. However, `CoditoOAuth2Validator.validate_scopes` accepts only
`MCP_TOOL_SCOPES` for an approved ChatGPT CIMD client. These identity scopes are
therefore advertised but rejected for that client.

The official [OpenAI authentication guide, OIDC scopes section](https://developers.openai.com/plugins/build/auth)
states that ChatGPT requests advertised OIDC scopes by default. Source accessed
2026-09-08. Combined with the rejection below, this is a concrete compatibility
defect and the leading explanation for the reported connection failure.

Read-only probes invoked the deployed `AuthorizationView` GET handler with the
existing ChatGPT application, active link and account, exact registered callback,
resource indicator, and S256 challenge. No consent was submitted and no tokens
were minted. The three requests differed only in scope:

| Requested scopes | HTTP result | OAuth result |
|---|---|---|
| All five MCP tool scopes | 200 | Consent page available |
| Same MCP scopes plus `openid profile` | 302 | Callback error `invalid_scope` |
| Scope parameter omitted | 302 | Callback error `invalid_scope` |

The omitted-scope case is another configuration inconsistency: DOT defaults to
all globally configured scopes, which also includes desktop-only `device:manage`.

Relevant code: `relay/codito_relay/core/views.py` discovery responses,
`relay/codito_relay/core/oauth.py` scope validation, and
`relay/codito_relay/settings.py` global OAuth scopes/defaults.

## Required remediation and verification

1. Align advertised identity scopes and approved MCP-client permissions while
   keeping `device:manage` strictly desktop-only. If OIDC remains advertised,
   configure and test CIMD ID-token signing and UserInfo behavior too; merely
   widening a set is not a complete OIDC fix.
2. Define client-specific default scopes, and advertise the intended MCP scopes
   in the unauthenticated challenge. Preserve exact resource/link/account checks.
3. Add regression coverage for advertised OIDC scopes, omitted scopes, the actual
   rendered consent form, token exchange, and signed client assertions. Keep
   negative coverage for MCP access to desktop-only permissions.
4. Add sanitized OAuth phase/status/error diagnostics. Do not log request bodies,
   authorization codes, assertions, cookies, tokens, or complete callback URLs.
5. Deploy a tested immutable relay image, retry the user's real ChatGPT connection,
   then verify tool discovery and a project file read through the connected agent.

The initial request's exact parameters were not retained. The later live retry
above supplies direct evidence of the scope rejection, but not proof that no
additional token-exchange issue exists.
