# ADR 0007: Locally governed project management tool

- Status: Accepted
- Date: 2026-09-08
- Supersedes: ADR 0004's exact tool-count decision only

## Context

The desktop UI can register and rename projects, but a ChatGPT workflow also needs
to request these lifecycle actions without learning or inventing a local absolute
path. Treating them as read or file-patch variants would make annotations and OAuth
scopes inaccurate.

## Decision

Add a fourth `project_manage` tool with `get_projects`, `request_add_project`,
`rename_project`, and `remove_project` operations. Mutations require
`projects:write`. Rename and remove require one-shot Windows approval.

`request_add_project` records a bounded, expiring local request. Its schema has no
path field. The Windows user selects a fixed local folder with the native picker;
only relay-safe metadata is synchronized afterward. Remove is metadata-only and
never deletes source files.

## Consequences

Existing clients continue to use the original three tools. Users must reauthorize
to grant `projects:write` before calling mutating project operations. The additional
tool improves annotation accuracy and keeps local path selection outside the model
and relay trust boundaries.
