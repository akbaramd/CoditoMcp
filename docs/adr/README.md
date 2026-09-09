# Architecture decision records

ADRs are immutable after acceptance except for typo/link repairs. A changed decision
adds a new ADR that marks the prior one superseded.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-python-modular-monolith.md) | Python modular monolith and Windows companion | Accepted |
| [0002](0002-oauth-and-device-identity.md) | Custom OAuth/OIDC and proof-bound device identity | Lifetimes superseded by 0016 |
| [0003](0003-windows-execution-boundary.md) | AppContainer default and explicit native modes | Accepted |
| [0004](0004-three-tool-surface.md) | Exactly three MCP tools | Superseded by 0007 |
| [0005](0005-durable-dispatch.md) | PostgreSQL truth, Redis routing, epoch fencing | Accepted |
| [0006](0006-local-path-privacy.md) | Absolute paths remain on device | Accepted |
| [0007](0007-locally-governed-project-management.md) | Locally governed project management | Accepted |
| [0013](0013-native-project-approvals.md) | Native project routing, persistent shell grants and actionable approvals | Accepted |
| [0014](0014-consented-screenshots.md) | Separate-consent primary-display screenshots | Partially superseded by 0015 |
| [0015](0015-selected-display-and-browser-consent.md) | Selected-display Always allow and bounded browser opening | Accepted |
| [0016](0016-oauth-refresh-continuity.md) | Bounded longer OAuth continuity and safe concurrent refresh | Accepted |
| [0017](0017-hybrid-durable-async-operations.md) | Hybrid HTTP/WSS durable async operations and approval continuation | Accepted |
| [0018](0018-focused-tools-and-local-access.md) | Focused tool names, direct commands, three local modes and exact saved permissions | Implementation and verification |
| [0019](0019-code-intelligence.md) | Language-neutral LSP + structural graph Code Intelligence with snapshot freshness | Accepted |
| [0020](0020-managed-frontend-inspection.md) | Agent-owned loopback dev server + Chromium frontend inspection | Accepted for 0.3.0 |
