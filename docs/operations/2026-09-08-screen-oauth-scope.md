# Screenshot OAuth grant diagnosis

Accessed 2026-09-08. Never export raw access/refresh tokens or screenshot pixels.

## Confirmed evidence

- Windows daemon/tray and relay run 0.1.9. Public readiness and WSS are healthy.
- Actual MCP read operation `bd9ed6d4-7a08-4eab-abb7-a41587cce818`
  was bound to the existing conversation's grant, whose active token row 37
  lacks `screen:read`. New, distinct grants (rows 34, 36, 38) include it.
- This proves that a successful new login does not upgrade the old grant used
  by this conversation. It does not prove which token every other chat uses.
- No screenshot operation reached durable dispatch during that inspection.
- `device_screenshot` is registered with `securitySchemes` requiring `screen:read`.
- The missing-scope tool result had no `_meta["mcp/www_authenticate"]`. It is
  returned over HTTP 200, so the gateway's unauthenticated HTTP 401 cannot help.

## Correction

Return an MCP result-level Bearer challenge for `insufficient_scope`, with
path-specific resource metadata, an error description, and the union of current
approved scopes and the operation's required scopes. Desktop enrollment scope
is excluded. No permission is upgraded, token deleted, or Windows screen
approval bypassed. Log the database token record ID, tool and missing scope,
never credentials, command bodies, URLs from requests, or private pixels.

Tests cover the serialized HTTP/SDK response, no dispatch/no token mutation,
no challenge for invalid input, and no reauthorization loop for an authorized
but offline device.

Source: [OpenAI authentication: triggering the authentication UI](https://developers.openai.com/plugins/build/auth#triggering-authentication-ui).
The documented flow requires resource/tool metadata **and** an error result
carrying `mcp/www_authenticate`; declaring scopes alone is insufficient.
