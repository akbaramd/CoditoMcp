# Immutable relay rollback

Rollback changes the relay image to a previously recorded version/SHA or digest. It
does not reverse database migrations automatically.

1. Stop new operation admission and inspect active mutation/shell state.
2. Back up PostgreSQL and record the failing/current and target image digests.
3. Confirm schema compatibility. If the old code cannot read the current expanded
   schema, use a forward fix; never guess or delete migration rows.
4. Set `CODITO_RELAY_IMAGE` in the mode-0600 server `.env` to the previous immutable
   tag/digest.
5. Pull and start relay only, keeping PostgreSQL/Redis volumes intact.
6. Verify internal readiness, metadata/resource URLs, OAuth grant/token behavior,
   WSS reconnect/epoch fencing, and an authorized read.
7. Reconcile accepted/non-terminal operations. Never replay shell starts.
8. Record cause, timeline, data impact, and follow-up test.

If compromise is suspected, a normal rollback is insufficient: revoke signing
keys/tokens/device tickets as appropriate, isolate the host, preserve evidence, and
follow the incident runbook.
