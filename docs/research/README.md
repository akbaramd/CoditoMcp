# Research index

Last reviewed: **2026-09-09**.

This bibliography captures the primary sources used for the MVP decisions. The
documents describe the intended design; automated tests and deployment evidence
determine whether a control is actually active.

## OpenAI and MCP

| Subject | Primary source | Accessed | Decision supported |
|---|---|---:|---|
| Custom MCP authentication | [OpenAI — Authentication](https://developers.openai.com/plugins/build/auth) | 2026-09-08 | OAuth 2.1, PKCE, protected-resource discovery, per-tool schemes, and resource audience binding |
| Connect from ChatGPT | [OpenAI — Connect from ChatGPT](https://developers.openai.com/plugins/deploy/connect-chatgpt) | 2026-09-08 | Private developer-mode MCP URL and HTTPS deployment flow |
| Build an MCP server | [OpenAI — Build your MCP server](https://developers.openai.com/plugins/build/mcp-server) | 2026-09-08 | Tool metadata and remote server expectations |
| Tool names and descriptions | [OpenAI — Optimize metadata](https://developers.openai.com/plugins/guides/optimize-metadata) | 2026-09-08 | Focused action-oriented tool surface; ADR 0018 |
| Invocation status titles | [OpenAI — Apps reference](https://developers.openai.com/plugins/reference) | 2026-09-08 | Human-readable title plus static invoking/invoked status, maximum 64 characters |
| Structured tool results | [OpenAI — Apps reference](https://developers.openai.com/plugins/reference) | 2026-09-09 | Every tool returning `structuredContent` declares an exact `outputSchema` |
| Python implementation | [Model Context Protocol — Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 2026-09-08 | Official SDK v2 and Streamable HTTP transport |
| Authorization | [MCP Authorization specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) | 2026-09-08 | OAuth discovery, resource indicators, and token audience validation |

## Identity and web security

| Subject | Primary source | Accessed | Decision supported |
|---|---|---:|---|
| Django support policy | [Django 5.2 release notes](https://docs.djangoproject.com/en/5.2/releases/5.2/) | 2026-09-08 | 5.2 LTS modular monolith |
| OAuth toolkit | [Django OAuth Toolkit documentation](https://django-oauth-toolkit.readthedocs.io/en/latest/) | 2026-09-08 | OAuth provider, MCP metadata, PKCE, resource indicators, CIMD, token management |
| OAuth authorization server metadata | [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) | 2026-09-08 | Discovery metadata |
| Protected resource metadata | [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) | 2026-09-08 | Path-specific resource discovery |
| Resource indicators | [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) | 2026-09-08 | Exact device-link audience |
| PKCE | [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636) | 2026-09-08 | S256 authorization-code binding |
| OAuth security practices | [RFC 9700](https://www.rfc-editor.org/rfc/rfc9700) | 2026-09-08 | Redirect matching, refresh rotation, sender/client controls |
| Client ID metadata documents | [IETF OAuth CIMD draft](https://datatracker.ietf.org/doc/draft-ietf-oauth-client-id-metadata-document/) | 2026-09-08 | ChatGPT client metadata without open registration |
| Password storage | [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) | 2026-09-08 | Argon2id and upgradeable parameters |
| Session security | [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) | 2026-09-08 | Cookie flags, rotation, idle and absolute expiry |

## Windows isolation and local security

| Subject | Primary source | Accessed | Decision supported |
|---|---|---:|---|
| AppContainer isolation | [Microsoft — AppContainer isolation](https://learn.microsoft.com/windows/win32/secauthz/appcontainer-isolation) | 2026-09-08 | Per-project low-privilege command boundary |
| AppContainer profiles | [Microsoft — AppContainer profiles](https://learn.microsoft.com/windows/win32/secauthz/appcontainer-for-legacy-applications-) | 2026-09-08 | Profile and capability lifecycle |
| Job Objects | [Microsoft — Job Objects](https://learn.microsoft.com/windows/win32/procthread/job-objects) | 2026-09-08 | Process-tree containment and kill-on-close |
| File handles | [Microsoft — CreateFile](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew) | 2026-09-08 | Handle-relative validation and reparse controls |
| File identity | [Microsoft — GetFileInformationByHandleEx](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandleex) | 2026-09-08 | Volume/file identity and post-open verification |
| Atomic replacement | [Microsoft — ReplaceFile](https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-replacefilew) | 2026-09-08 | Same-volume atomic file replacement |
| CNG key storage | [Microsoft — CNG Key Storage Providers](https://learn.microsoft.com/windows/win32/seccng/key-storage-and-retrieval) | 2026-09-08 | Non-exportable device key and platform provider |
| DPAPI | [Microsoft — CryptProtectData](https://learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) | 2026-09-08 | Per-user protected fallback material |
| Windows notifications | [Microsoft — App notifications overview](https://learn.microsoft.com/windows/apps/design/shell/tiles-and-notifications/) | 2026-09-08 | Notification-to-dialog approval UX |

## Runtime, reliability, and operations

| Subject | Primary source | Accessed | Decision supported |
|---|---|---:|---|
| WebSocket protocol | [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455) | 2026-09-08 | Outbound full-duplex tunnel |
| Redis Streams | [Redis Streams documentation](https://redis.io/docs/latest/develop/data-types/streams/) | 2026-09-08 | Consumer-group dispatch and replayable notifications |
| PostgreSQL durability | [PostgreSQL continuous archiving](https://www.postgresql.org/docs/current/continuous-archiving.html) | 2026-09-08 | Backup limitations and later off-host/PITR path |
| Compose | [Docker Compose production guidance](https://docs.docker.com/compose/how-tos/production/) | 2026-09-08 | Health checks, restart policy, and immutable images |
| OpenTelemetry | [OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/) | 2026-09-08 | Trace and metric export |
| Prometheus exposition | [Prometheus exposition formats](https://prometheus.io/docs/instrumenting/exposition_formats/) | 2026-09-08 | `/metrics` endpoint |
| GitHub release API | [GitHub — Get the latest release](https://docs.github.com/en/rest/releases/releases#get-the-latest-release) | 2026-09-08 | Stable release discovery and GitHub-published asset digest |
| Build supply chain | [GitHub — Secure your build system](https://docs.github.com/en/code-security/tutorials/implement-supply-chain-best-practices/securing-builds) | 2026-09-08 | Narrow release permissions and separate unsigned previews |
| Bootstrap reference | [MateMCP bootstrap-windows.ps1](https://github.com/vrassouli/MateMCP/blob/main/scripts/bootstrap-windows.ps1) | 2026-09-08 | User-supplied reference for one-command per-user installation; Codito adds release-digest verification |

## Research conclusions

- A route identifier is not an authorization credential. Every device-specific MCP
  request requires OAuth and exact grant/resource/scope/account/device checks.
- Job Objects bound process lifetime but do not establish a filesystem or network
  sandbox. AppContainer and handle-safe path operations are separate controls.
- WebSocket availability cannot be guaranteed while Windows sleeps, powers off, or
  lacks networking. The contract is bounded recovery with explicit offline and
  uncertain outcomes.
- A same-disk backup is useful for operator error but does not protect against disk
  or host loss. That risk is accepted only for MVP.
