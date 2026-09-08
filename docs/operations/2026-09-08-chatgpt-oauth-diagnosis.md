# ChatGPT OAuth connection failure — 2026-09-08

Status: reproducible authorization defect identified; no production fix applied
in this diagnostic pass. A real ChatGPT retry is still required after remediation.

## Production observations

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

The failing user request's exact parameters were not retained in current logs;
the reproduction must not be presented as a captured trace of that request or
as proof that no additional token-exchange issue exists.
