# codito-protocol

This package is the only shared wire-contract dependency between `codito-relay`
and `codito-agent`. It contains Pydantic v2 models, canonical action digesting,
MCP tool metadata, and JSON Schema export. It intentionally contains no database,
network, UI, or filesystem implementation.

Public inputs never contain local absolute paths, approval decisions, or trust
flags. Those are resolved from authenticated server state and local device policy.

Protocol paths use forward slashes and the empty string for the project root.
`ProjectApplyPatchInput.base_hashes` contains every touched path: a SHA-256 value
means the current file must match, while `null` means the path must not exist (the
safe precondition for an Add or Move destination).
