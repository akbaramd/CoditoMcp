# ADR 0003: AppContainer by default and explicit native modes

- Status: Accepted
- Date: 2026-09-08

## Context

A command can escape its working directory even when the process starts inside a
project. Python path checks and Job Objects alone cannot provide filesystem and
network confinement. Some development commands nevertheless need normal user
capabilities.

## Decision

Each project has one device-local mode:

1. `isolated` (default): per-project AppContainer with only the root granted, no
   network capability, sanitized environment, no inherited handles, and a
   kill-on-close Job Object. Valid isolated commands do not prompt.
2. `native_approval`: run as the logged-in user after a signed, one-shot local
   approval showing the exact resolved action and risk.
3. `native_trusted`: explicit local opt-in after a strong warning; run host commands
   without prompts for that project.

The broker self-test is mandatory after install/update and resume. Any uncertainty,
profile/ACL error, containment failure, or broker authentication failure causes
`sandbox_unavailable`; there is no native fallback. Elevation, PTY, interactive
input, detached processes, and GUI launch are absent in v1.

## Consequences

`native_trusted` grants the command all filesystem and network authority held by the
user. Codito must not claim it can detect every escape. Approval UI, audit records,
and documentation distinguish local project selection from OS-level confinement.
