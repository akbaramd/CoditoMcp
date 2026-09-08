# Accepted MVP limits

These are explicit scope boundaries, not hidden defects.

Current tool/access definitions are in [ADR 0018](adr/0018-focused-tools-and-local-access.md).
The selectable Full device access mode uses a new `full_access` value; persisted
legacy trust is never upgraded automatically. Project access is cooperative native
approval routing, not OS confinement. Device-wide OAuth authorizes project selection
per call; there is no hidden conversation-wide single-project binding.

- Windows 11 x64 is the only supported desktop. Protocol/core remain portable for a
  later macOS agent, but no macOS security claim exists.
- Private device-specific MCP links in ChatGPT developer mode are supported; public
  plugin review/directory publication is not.
- Accounts are invite-only and links are delivered manually. Without SMTP, opening
  an email-bound invite proves possession of the link, not independent mailbox
  control. Recovery is an admin-created single-use 15-minute link.
- Local absolute project paths stay exclusively on the device.
- Only fixed local NTFS/ReFS project roots are accepted. UNC, removable media,
  drive roots, cloud placeholders, reparse roots, hardlink mutation, ADS, and device
  namespaces are rejected.
- `full_access` deliberately grants the logged-in user's native file/shell/desktop
  authority without local prompts. Working directory is not containment. Legacy
  `native_trusted` requires explicit reselection for this new broader policy.
- No elevation, Windows service, interactive terminal, PTY/stdin, detached process,
  remote GUI, or offline mutation queue exists.
- A sleeping, powered-off, or disconnected device cannot stay online. Codito offers
  automatic recovery and honest offline/uncertain states, not uninterrupted access.
- Backups are local to `/data` for MVP. They do not survive loss/compromise of that
  disk/host. Encrypted off-host backup is required after MVP.
- DNS, public TLS, Nginx, and OPNsense are owner-managed. Codito binds only
  `192.168.200.39:8094` and derives URLs from the public HTTPS base.
- Production Windows signing activates only when a certificate is supplied;
  unsigned test installers display Windows warnings.
- The initial relay is one Docker host with one PostgreSQL and Redis instance. Two
  ASGI workers avoid sticky MCP state but do not provide host-level high availability.
