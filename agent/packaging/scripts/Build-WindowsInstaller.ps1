[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '0.3.1',
    [string]$OutputDirectory,
    [string]$PayloadDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptDirectory = Split-Path -Parent $PSCommandPath
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory '..\..\..'))
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repositoryRoot 'installer-output'
}
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
if (-not $PayloadDirectory) {
    $PayloadDirectory = Join-Path $outputRoot "staging\Codito-$Version-win-x64-dev"
}
$payloadRoot = [System.IO.Path]::GetFullPath($PayloadDirectory)
$repositoryPrefix = $repositoryRoot.TrimEnd('\') + '\'
foreach ($candidate in @($outputRoot, $payloadRoot)) {
    if (-not $candidate.StartsWith($repositoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Packaging paths must remain inside the repository: $candidate"
    }
}
if (-not $IsWindows) {
    throw 'The Codito MSI must be built on Windows.'
}
if (-not (Test-Path -LiteralPath (Join-Path $payloadRoot 'SHA256SUMS') -PathType Leaf)) {
    throw 'Build and verify the unsigned developer payload before building the MSI.'
}
if ($env:CODITO_WIX_OSMF_REVIEWED -ne '1') {
    throw @'
WiX Toolset v6.0.2 is subject to the Open Source Maintenance Fee terms.
Codito does not accept those terms on your behalf. Review
https://docs.firegiant.com/wix/osmf/ and, only when authorized to proceed, set
CODITO_WIX_OSMF_REVIEWED=1 for this build process.
'@
}

$wixProject = Join-Path $repositoryRoot 'agent\packaging\wix\Codito.Agent.wixproj'
$wixOutput = Join-Path $outputRoot 'wix'
New-Item -ItemType Directory -Path $wixOutput -Force | Out-Null
& dotnet build $wixProject `
    --configuration Release `
    -p:PackageVersion=$Version `
    -p:PayloadDir=$payloadRoot `
    -p:BaseOutputPath=$wixOutput `
    -p:ContinuousIntegrationBuild=true
if ($LASTEXITCODE -ne 0) {
    throw 'WiX installer build failed.'
}

$builtMsi = Get-ChildItem -LiteralPath $wixOutput -Filter '*.msi' -File -Recurse |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
if ($null -eq $builtMsi) {
    throw 'WiX completed without producing an MSI.'
}
$msiPath = Join-Path $outputRoot "Codito-$Version-win-x64-unsigned.msi"
Copy-Item -LiteralPath $builtMsi.FullName -Destination $msiPath -Force
$msiHash = (Get-FileHash -LiteralPath $msiPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(
    "$msiPath.sha256",
    "$msiHash  $([System.IO.Path]::GetFileName($msiPath))`n",
    [System.Text.UTF8Encoding]::new($false)
)
Write-Output $msiPath
Write-Output "$msiPath.sha256"
