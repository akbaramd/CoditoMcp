# Windows packaging and release

The Windows release is a per-user application, never a service. Python 3.12 and
.NET 8 are bundled, so a target Windows 11 x64 machine needs neither runtime.

## Locked toolchain

- Python is provisioned by `uv`; `uv.lock` is mandatory.
- PyInstaller is exactly `6.22.2` in the agent's `package` extra.
- The broker is published self-contained, single-file for `win-x64`.
- WiX is the MSBuild SDK exactly `6.0.2` in
  `agent/packaging/wix/Codito.Agent.wixproj`.

WiX v6 binary releases are subject to the Open Source Maintenance Fee terms. An
authorized person must review [WiX OSMF](https://docs.firegiant.com/wix/osmf/)
before downloading or using the SDK. Codito neither accepts those terms nor makes
the revenue/fee determination for the user. The MSI build remains disabled until
that person sets `CODITO_WIX_OSMF_REVIEWED=1` for the build process. This is a
local acknowledgement gate, not an automated EULA acceptance switch.

## Unsigned developer ZIP

From a clean Windows 11 x64 checkout:

```powershell
uv sync --frozen --package codito-agent --extra ui --extra package
dotnet test agent/broker/Codito.Broker.sln --configuration Release --maxcpucount:1
./agent/packaging/scripts/Test-PackagingConfig.ps1
./agent/packaging/scripts/Build-WindowsDevPackage.ps1 -Version 0.1.1
./agent/packaging/scripts/Test-WindowsDevPackage.ps1 `
    -ArchivePath installer-output/Codito-0.1.1-win-x64-dev.zip
```

The build produces `installer-output/Codito-0.1.1-win-x64-dev.zip` and its outer
`.sha256` file. The ZIP contains a sorted `SHA256SUMS` manifest, three separate
entrypoints, and the broker:

- `cli/codito-agent.exe` has a console for enrollment and project management.
- `daemon/codito-agent-daemon.exe` is windowless and discovers the sibling broker.
- `tray/codito-agent-tray.exe` is the windowless PySide6 tray/approval UI.
- `broker/Codito.Broker.exe` is the exact self-contained broker used by the daemon.

The archive writer sorts entries and gives them a fixed timestamp. The build also
sets `SOURCE_DATE_EPOCH` and `PYTHONHASHSEED`; identical source and locked tools
are therefore the reproducibility boundary. Always compare the published outer
hash and the inner manifest instead of trusting a filename.

## GitHub release and bootstrap

Every ordinary push to `main` runs `.github/workflows/windows-release.yml` on a
GitHub-hosted Windows runner. If the repository declares a valid SemVer greater than
the latest release tag (for example `0.2.0` after `v0.1.14`), that explicit version is
released. Otherwise the workflow increments the latest patch version. It updates the
version files and lockfile, repeats the release gates, commits/tags the synchronized
version, and publishes an unsigned stable ZIP. It can be installed with:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/akbaramd/CoditoMcp/main/scripts/bootstrap-windows.ps1)))
```

The bootstrap accepts only an HTTPS asset hosted by GitHub whose exact filename
matches the release type and whose GitHub release metadata contains a SHA-256
digest. The package installer verifies the inner `SHA256SUMS`, stages a versioned
copy below `%LOCALAPPDATA%\Codito\app`, and changes only the current user's startup
entries. The package includes Python and .NET; it requests neither elevation nor a
system-wide runtime installation.

## In-app update checks

The desktop Update Center checks only the latest stable GitHub release. Automatic
installation is enabled by default and runs at startup and every six hours, using
the same validation path as a manual check. The updater:

1. accepts only exact stable `vX.Y.Z` releases and `Codito-X.Y.Z-win-x64.zip`;
2. verifies the asset host, size, GitHub-published digest, downloaded SHA-256, ZIP
   paths, link attributes, entry count, expanded size, and compression ratio;
3. extracts into a fresh temporary directory and invokes the package's fixed-name
   installer through the System32 Windows PowerShell executable.

The updater never executes a release note, alternate asset name, redirect-selected
script, or model-provided command. Production code signing remains required to
remove Windows publisher warnings; GitHub release and SHA-256 verification still
apply to unsigned MVP packages.

## Per-user MSI

After the authorized WiX/OSMF review:

```powershell
$env:CODITO_WIX_OSMF_REVIEWED = "1"
./agent/packaging/scripts/Build-WindowsInstaller.ps1 -Version 0.1.1
Remove-Item Env:CODITO_WIX_OSMF_REVIEWED
```

The MSI installs below `%LOCALAPPDATA%\Programs\Codito`, writes only current-user
startup entries for both daemon and tray, and creates no service or elevation
request. Uninstall removes installed payload/startup entries but deliberately
preserves `%LOCALAPPDATA%\Codito` state. Device revocation is a separate explicit
security action; silently deleting local data would not revoke the remote grant.

Before upgrade or uninstall, exit the tray and daemon. The MVP authoring relies on
Windows Installer's in-use-file handling and does not ship a process-killing custom
action. A restart may be requested if a process keeps an installed file open.

## Signing and release gates

`Sign-WindowsArtifacts.ps1` is an explicit Authenticode hook. It requires a
certificate-store thumbprint and HTTPS timestamp URL, signs PE files before MSI,
and verifies every signature. It never reads a PFX or embeds signing material.

Before publishing a production artifact:

1. Run Python quality gates and broker analyzers/xUnit.
2. Build the developer payload and validate both SHA-256 manifests.
3. On a clean Windows 11 VM, test enrollment, packaged broker probe, tray approval,
   read/patch/shell lifecycle, upgrade, uninstall, and retained user state.
4. Confirm the broker's isolated-mode self-test passes. Until AppContainer ACL and
   network-denial proof exists, isolated shell correctly fails closed.
5. Sign broker, Python executables/DLLs, and MSI with timestamping; then rebuild the
   manifests over the signed bytes.
6. Produce SBOM/provenance and publish only versioned artifacts.

Unsigned ZIP/MSI files are test artifacts and will show Windows trust warnings.
Production signing remains blocked until a code-signing certificate is supplied.
