# Windows approval and screenshot release verification

## Diagnosed evidence

- Installed 0.1.7 local history had `approval_expired` for shell requests at
  11:48:56Z and 11:50:01Z, plus an earlier `sandbox_unavailable`.
- The tray received a destructively dequeued request, without display acknowledgement.
- Native shell approval blocked the initial MCP call inside its 45-second deadline.
- Old notifications were text-only QSystemTrayIcon balloons.
- New Qt test exposed `setDetailedText` removing the AlwaysOnTop flag.

## Local acceptance commands

```powershell
uv run ruff check .
uv run ruff format --check .
$env:MYPYPATH='packages/protocol/src;relay;agent/src'
uv run mypy -p codito_protocol -p codito_agent -p codito_relay
$env:DJANGO_SETTINGS_MODULE='codito_relay.test_settings'
uv run pytest agent/tests packages/protocol/tests relay/tests -q
uv run pytest packages/protocol/tests --cov=codito_protocol --cov-fail-under=90
dotnet test agent/broker/Codito.Broker.sln --configuration Release --maxcpucount:1
```

## Windows/ChatGPT acceptance

1. Confirm both daemon and tray display the release version and the relay is online.
2. Projects → select **Project access (native)** and read the native-authority warning.
3. Run a project build/tool version without prompting. Verify `uv`, `dotnet`, `git`,
   `ssh` and `docker` resolve where installed. Docker additionally requires its daemon;
   SSH requires existing keys/host trust. No password input, PTY, elevation or detached GUI.
4. Request external cwd C:/ or a literal external path. Start must return pending_approval;
   native toast and persistent dialog must offer Deny/Allow/Always allow.
5. Deny: no process. Allow: one process. Always: same request scope is remembered;
   different scope asks. Revoke in Settings and confirm a new prompt.
6. In a disposable registered folder, add/read/anchored-update/move/delete a test file.
   Hash conflicts and ambiguous anchors must still fail without partial mutation.
7. Refresh/re-authorize the ChatGPT connection for the new screen:read scope. Invoke
   device_screenshot, approve locally, and verify a current PNG renders in ChatGPT.
   Read/shell saved grants must never suppress this separate screenshot prompt.
8. Restart/reconnect, cancel a pending job, revoke the link, and verify there is no
   replayed shell start, stale approval acceptance or cross-binding output.

Local diagnostics: `%LOCALAPPDATA%\Codito\Codito\logs\desktop.jsonl` and `daemon.jsonl`,
1 MiB each with three rotations. Review creation, display, decision and toast events;
do not export tokens, command bodies or unrelated private desktop content.

Release/deployment evidence is appended after actual verification, not assumed here.
