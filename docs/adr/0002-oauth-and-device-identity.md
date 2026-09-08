# ADR 0002: Custom OAuth/OIDC and proof-bound device identity

- Status: Accepted
- Date: 2026-09-08

## Context

An unguessable MCP link cannot prove who is calling it. ChatGPT requires standards-
based authorization discovery and the desktop agent needs revocable enrollment
without a client secret embedded in the binary. The MVP has no SMTP provider.

## Decision

- Django owns invite-only email/password accounts; Argon2id hashes passwords.
- Django OAuth Toolkit is the authorization server. Access tokens are opaque,
  database-backed, resource-bound, and live 15 minutes. Refresh tokens rotate,
  live 30 days, and reuse revokes the family.
- ChatGPT uses authorization code + PKCE S256 and a Client ID Metadata Document
  with `private_key_jwt`; open dynamic client registration is disabled.
- Redirect URIs are compared exactly and the `resource` value must exactly equal
  `https://codito.akbaramd.ir/mcp/d/{link_id}`.
- The desktop is a pre-registered public client using PKCE and a loopback callback.
- Enrollment registers a locally generated non-exportable CNG key, TPM-backed when
  supported. A DPAPI-protected software-key fallback is explicitly marked in UI.
- Device WebSocket tickets are short-lived, single-use, and require proof by that
  key. Link IDs route but do not authorize.

## Consequences

The authorization server must expose RFC 8414 and path-specific RFC 9728 metadata,
validate tenant/grant/device/link/project/resource/scope on every tool call, rotate
signing keys, and retain revocation state. Manual invitation delivery proves control
of the invitation URL, not ownership of the email address; the UI must state this.
