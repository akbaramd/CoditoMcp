# PostgreSQL backup and restore

The accepted MVP backup target is `/data/backups/codito` on the same server. This
protects against many application/operator mistakes but **not** loss or compromise
of the `/data` disk or host. Off-host encrypted backup is a release-follow-up.

Redis is not restored as authoritative state. Device connections reconnect with new
epochs and live routing is rebuilt from PostgreSQL plus agent journals.

## Schedule and retention

Run `/data/projects/codito/scripts/backup.sh` daily from a root-owned systemd timer or
cron entry. The script uses a PostgreSQL custom-format dump from the existing
`postgres` container, SHA-256 sidecar, mode 0600 via umask, atomic rename, seven-day
daily retention, and four-week Sunday retention. It dumps only the dedicated
`codito` database and never modifies another application's database.

Example cron (choose a quiet UTC time and monitor it):

```cron
17 1 * * * /data/projects/codito/scripts/backup.sh >>/var/log/codito-backup.log 2>&1
```

Success means more than exit zero: alert if the latest dump/sidecar is absent,
empty, too old, has a bad checksum, or storage is low. Dumps contain account and
OAuth data and require the same protection as production.

## Restore drill

Perform at least once before launch and quarterly thereafter, preferably into an
isolated test stack:

1. Select an absolute dump path and verify its `.sha256` without modifying it.
2. Record current image tag, Git SHA, schema migration head, row counts, and device/
   link revocation counts.
3. Stop relay admission. Keep a copy of the pre-restore database volume/dump until
   verification completes.
4. Set `CONFIRM_RESTORE=codito` and invoke `restore.sh /absolute/dump`.
5. Run migrations from the image version compatible with that dump.
6. Verify readiness, admin login, OAuth metadata/JWKS, token revocation and audience,
   link/device/project ownership counts, audit ordering, and operation terminal
   states.
7. The restore script flushes the dedicated Redis database; restart relay workers
   and require agents to reconnect with new epochs so routing is rebuilt.
8. Revoke any credential whose post-backup issuance/revocation state is uncertain.
9. Record duration, recovery point, evidence, discrepancies, and remediation.

The script uses `pg_restore --clean --if-exists` and is destructive to the target
database. Never run it against an unverified target or without the explicit
confirmation environment value.

## Recovery expectations

- Target recovery point: most recent successful daily dump (up to 24 hours).
- Target restore time for MVP: four hours after infrastructure is available.
- OAuth/device/link changes after the dump may be lost; fail safe by revoking and
  re-enrolling when uncertain.
- Operations restored in non-terminal states are reconciled, not blindly replayed.
  Shell starts become `outcome_unknown`; patches use journals/hashes; reads may retry
  only within a fresh authorized request.
