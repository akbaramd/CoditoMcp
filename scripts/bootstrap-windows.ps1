[CmdletBinding()]
param(
    [string]$Repository = 'akbaramd/CoditoMcp',
    [string]$VersionTag,
    [switch]$AllowPrerelease
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
    -not [Environment]::Is64BitOperatingSystem) {
    throw 'Codito requires 64-bit Windows 11.'
}
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw 'Repository must use the owner/name format.'
}

$headers = @{
    Accept = 'application/vnd.github+json'
    'X-GitHub-Api-Version' = '2026-03-10'
    'User-Agent' = 'Codito-Windows-Bootstrap'
}
$endpoint = if ($VersionTag) {
    "https://api.github.com/repos/$Repository/releases/tags/$VersionTag"
} else {
    "https://api.github.com/repos/$Repository/releases/latest"
}
$release = Invoke-RestMethod -Uri $endpoint -Headers $headers
if ($release.draft -or ($release.prerelease -and -not $AllowPrerelease)) {
    throw 'Refusing to bootstrap a draft or prerelease build without -AllowPrerelease.'
}
$version = ([string]$release.tag_name).TrimStart('v')
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Unsupported release version: $($release.tag_name)"
}
$assetName = if ($release.prerelease) {
    "Codito-$version-win-x64-dev.zip"
} else {
    "Codito-$version-win-x64.zip"
}
$asset = @($release.assets | Where-Object { $_.name -eq $assetName })
if ($asset.Count -ne 1) {
    throw "Release must contain exactly one $assetName asset."
}
$digest = [string]$asset[0].digest
if ($digest -notmatch '^sha256:([a-f0-9]{64})$') {
    throw 'Release asset does not contain a trusted SHA-256 digest.'
}
$expectedHash = $Matches[1]
$downloadUrl = [Uri]$asset[0].browser_download_url
if ($downloadUrl.Scheme -ne 'https' -or $downloadUrl.Host -ne 'github.com') {
    throw 'Release download URL is not an approved GitHub HTTPS URL.'
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("codito-install-" + [Guid]::NewGuid().ToString('N'))
$archive = Join-Path $tempRoot $assetName
$extractRoot = Join-Path $tempRoot 'package'
New-Item -ItemType Directory -Path $tempRoot, $extractRoot -Force | Out-Null
try {
    Invoke-WebRequest -Uri $downloadUrl.AbsoluteUri -Headers $headers -OutFile $archive
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw 'Downloaded release digest does not match GitHub release metadata.'
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $extractRoot
    $roots = @(Get-ChildItem -LiteralPath $extractRoot -Directory)
    if ($roots.Count -ne 1) {
        throw 'Release archive must contain exactly one package directory.'
    }
    $installer = Join-Path $roots[0].FullName 'install-windows.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw 'Release package does not contain install-windows.ps1.'
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
    if ($LASTEXITCODE -ne 0) {
        throw "Codito installer exited with code $LASTEXITCODE."
    }
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
