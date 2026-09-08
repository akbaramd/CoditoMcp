# Approved reads outside registered projects

Date: 2026-09-08. Status: implemented; release verification tracked separately.

The user explicitly replaced the projects-only read restriction: a request to
inspect C: must reach Windows consent, not be refused solely because C: is not a
project. The user confirmed that Always allow means reading the selected directory;
editing and execution must not inherit that permission.

`device_read` adds bounded directory listings and text reads. A drive root may be
requested. It is not registered as a project and cannot be passed to patch/shell
tools. Existing project tools and native execution policies remain unchanged.

Consent is Deny, Allow once (the exact request), or Always allow reading the shown
directory and descendants. Persistent grants are local SQLite records bound to
account, OAuth grant family, device, link, canonical requested scope and root file
identity. They survive reconnects/restarts, not sign-out/re-enrollment. A different
link, account, grant or root identity requires new consent. Settings can revoke all
saved read permissions. Pending/in-flight results are invalidated by connection or
security changes and local revocation. The model cannot create a grant.

This intentionally changes the old absolute-path privacy contract only for this
tool: the user-requested scope and returned names/content travel through the relay
to the requesting MCP client. Operation journals contain those requests/results;
sanitized audit logs must not contain file contents or paths. Approval warns that
wide scopes can include private files/secrets. Windows user ACLs still apply; there
is no elevation. Do not mistake consent for permission to bypass OS protections.

On Windows, pin each path component with no write/delete sharing and reject reparse
points, placeholders, non-local volumes, aliases and hardlinks. Read from the
validated file handle, not a subsequently reopened pathname. Fail closed on other
platforms. Enumerations are non-recursive, bounded, and paginated; no automatic full
drive scan. A changing directory can change pagination ordering.

Deployment requires both relay and desktop support. Older desktops return a typed
unsupported-tool error; updating the relay alone cannot show the new Windows UI.
ChatGPT may need to refresh/reconnect its tool list after deployment.

## Installed Windows tools and explicit native execution

The user also requires the installed Windows developer tools, not a stripped
environment that cannot find uv/dotnet. `project_shell.start` accepts
`execution=native_approval`. This requests one-shot consent even for an isolated
project; it never changes the project's locally selected mode. No sandbox failure
silently starts a native process. Native jobs inherit standard developer variables
and the current machine/user registry PATH, excluding arbitrary daemon secrets.
The isolated environment remains sanitized. Native scripts may access other paths
or use Set-Location after the full-user-authority approval. A cwd check cannot
classify arbitrary shell commands as project-confined. Native trusted remains a
separate explicit local opt-in; it is not created by a read approval.

Reference checked 2026-09-08:
[Microsoft CreateFile sharing and reparse semantics](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).
