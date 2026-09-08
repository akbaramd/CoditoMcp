# Codito engineering record

This directory is the durable, version-controlled source of truth for Codito's
architecture, security posture, protocols, operating procedures, and release
readiness. Code changes that alter a public contract, trust boundary, persistent
state, or operational guarantee must update the relevant document and ADR in the
same pull request.

## Start here

- [Research index](research/README.md)
- [System architecture](architecture/system.md)
- [Protocol specification](protocol/tunnel.md)
- [MCP tool contracts](protocol/tools.md)
- [Threat model](security/threat-model.md)
- [Approval policy](security/approval-policy.md)
- [Deployment runbook](operations/deployment.md)
- [Backup and restore runbook](operations/backup-restore.md)
- [MVP checklist](mvp-checklist.md)
- [Latest verification evidence](verification/2026-09-08-mvp-scaffold.md)
- [Accepted MVP limits](accepted-limits.md)
- [Architecture decisions](adr/README.md)

## Documentation rules

1. Record an accessed date and primary URL for external facts.
2. Prefer standards and official product documentation over secondary sources.
3. Mark future work explicitly; do not document an unimplemented security control
   as active.
4. Diagrams are Mermaid source so they remain reviewable in Git.
5. A checked item in the MVP checklist requires a reproducible test or operating
   procedure.
