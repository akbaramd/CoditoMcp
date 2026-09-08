[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9A-Fa-f]{40}$')]
    [string]$CertificateThumbprint,
    [Parameter(Mandatory)]
    [ValidatePattern('^https://')]
    [string]$TimestampUrl,
    [Parameter(Mandatory)]
    [string]$ArtifactDirectory,
    [string]$SignToolPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$artifactRoot = [System.IO.Path]::GetFullPath($ArtifactDirectory)
if (-not (Test-Path -LiteralPath $artifactRoot -PathType Container)) {
    throw "Artifact directory does not exist: $artifactRoot"
}
if (-not $SignToolPath) {
    $signTool = Get-Command 'signtool.exe' -ErrorAction Stop
    $SignToolPath = $signTool.Source
}
if (-not (Test-Path -LiteralPath $SignToolPath -PathType Leaf)) {
    throw "signtool.exe does not exist: $SignToolPath"
}

$files = @(Get-ChildItem -LiteralPath $artifactRoot -File -Recurse |
    Where-Object { $_.Extension -in @('.exe', '.dll', '.msi') } |
    Sort-Object { if ($_.Extension -eq '.msi') { 1 } else { 0 } }, FullName)
if ($files.Count -eq 0) {
    throw 'No signable PE or MSI artifacts were found.'
}

foreach ($file in $files) {
    if ($PSCmdlet.ShouldProcess($file.FullName, 'Authenticode sign')) {
        & $SignToolPath sign `
            /sha1 $CertificateThumbprint `
            /fd SHA256 `
            /td SHA256 `
            /tr $TimestampUrl `
            /v `
            $file.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "Signing failed: $($file.FullName)"
        }
        & $SignToolPath verify /pa /all /v $file.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "Signature verification failed: $($file.FullName)"
        }
    }
}
