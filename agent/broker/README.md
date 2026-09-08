# Codito Windows broker

This self-contained .NET 8 process owns Windows primitives that the Python daemon
must not approximate: Job Objects now, and AppContainer/CNG/handle-relative file
operations as the broker matures.

`probe` deliberately reports `isolation_proven=false` until filesystem ACL and
network-denial self-tests exist. Consequently, the Python agent refuses isolated
execution and never falls back to native execution. Native execution is available
only for an explicitly trusted or individually approved project and still runs in
a kill-on-close, non-breakaway Job Object.

Structured execution resolves the executable against the sanitized environment
and rejects a direct Windows GUI-subsystem PE. Preventing a console program or an
explicit PowerShell/cmd script from spawning a GUI descendant requires a stronger
desktop/window-station isolation proof and remains a release blocker for claiming
general "no GUI" enforcement.

Build and test:

```powershell
dotnet test .\tests\Codito.Broker.Tests\Codito.Broker.Tests.csproj
dotnet publish .\src\Codito.Broker\Codito.Broker.csproj -c Release -o .\publish
```
