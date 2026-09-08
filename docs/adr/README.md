# Architecture decision records

ADRs are immutable after acceptance except for typo/link repairs. A changed decision
adds a new ADR that marks the prior one superseded.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-python-modular-monolith.md) | Python modular monolith and Windows companion | Accepted |
| [0002](0002-oauth-and-device-identity.md) | Custom OAuth/OIDC and proof-bound device identity | Accepted |
| [0003](0003-windows-execution-boundary.md) | AppContainer default and explicit native modes | Accepted |
| [0004](0004-three-tool-surface.md) | Exactly three MCP tools | Superseded by 0007 |
| [0005](0005-durable-dispatch.md) | PostgreSQL truth, Redis routing, epoch fencing | Accepted |
| [0006](0006-local-path-privacy.md) | Absolute paths remain on device | Accepted |
| [0007](0007-locally-governed-project-management.md) | Locally governed project management | Accepted |
| [0013](0013-native-project-approvals.md) | Native project routing, persistent shell grants and actionable approvals | Accepted |
| [0014](0014-consented-screenshots.md) | Separate-consent primary-display screenshots | Partially superseded by 0015 |
| [0015](0015-selected-display-and-browser-consent.md) | Selected-display Always allow and bounded browser opening | Accepted |
