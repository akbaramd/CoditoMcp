# Contributing

## Change discipline

1. Create or update an ADR for a changed trust boundary, external contract, or
   persistent-data rule.
2. Add failure-path tests before weakening or extending any filesystem, OAuth,
   approval, or process policy.
3. Keep absolute paths, command output, patch bodies, access tokens, cookies, and
   secrets out of logs and fixtures.
4. Run the local quality gate before opening a pull request.
5. Never deploy an image tagged `latest`; releases use `<version>-<git-sha>`.

Generated JSON Schemas must be refreshed after protocol changes:

```powershell
uv run --package codito-protocol python packages/protocol/scripts/export_schemas.py
```

Migration files are reviewed like application code. Relay startup does not run
migrations implicitly; deployment runs a one-shot migration job first.
