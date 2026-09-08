[CmdletBinding()]
param(
    [string]$PackageRoot = (Split-Path -Parent $PSCommandPath),
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $IsWindows -or -not [Environment]::Is64BitOperatingSystem) {
    throw 'Codito requires 64-bit Windows 11.'
}

$sourceRoot = [IO.Path]::GetFullPath($PackageRoot)
$versionPath = Join-Path $sourceRoot 'VERSION'
$manifestPath = Join-Path $sourceRoot 'SHA256SUMS'
if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw 'The Codito package is incomplete.'
}
$version = (Get-Content -LiteralPath $versionPath -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Invalid Codito package version: $version"
}

$requiredFiles = @(
    'cli/codito-agent.exe',
    'daemon/codito-agent-daemon.exe',
    'tray/codito-agent-tray.exe',
    'broker/Codito.Broker.exe'
)
$manifestEntries = @{}
foreach ($line in Get-Content -LiteralPath $manifestPath) {
    if (-not $line) { continue }
    if ($line -notmatch '^([a-f0-9]{64})  ([^\\]+)$') {
        throw 'SHA256SUMS contains an invalid entry.'
    }
    $relative = $Matches[2]
    $segments = $relative.Split('/')
    if ([IO.Path]::IsPathRooted($relative) -or $segments -contains '..' -or
        $segments -contains '.') {
        throw "Unsafe manifest path: $relative"
    }
    $manifestEntries[$relative] = $Matches[1]
}
foreach ($relative in $requiredFiles) {
    if (-not $manifestEntries.ContainsKey($relative)) {
        throw "Package manifest omits required file: $relative"
    }
}
foreach ($entry in $manifestEntries.GetEnumerator()) {
    $candidate = [IO.Path]::GetFullPath((Join-Path $sourceRoot $entry.Key))
    $prefix = $sourceRoot.TrimEnd('\') + '\'
    if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Manifest file is missing or outside the package: $($entry.Key)"
    }
    $actual = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $entry.Value) {
        throw "Package integrity check failed: $($entry.Key)"
    }
}

$productRoot = Join-Path $env:LOCALAPPDATA 'Codito'
$appsRoot = Join-Path $productRoot 'app'
$finalRoot = Join-Path $appsRoot $version
$stagingRoot = Join-Path $appsRoot (".$version-install-" + [Guid]::NewGuid().ToString('N'))
$appsPrefix = [IO.Path]::GetFullPath($appsRoot).TrimEnd('\') + '\'
foreach ($candidateRoot in @($finalRoot, $stagingRoot)) {
    $resolved = [IO.Path]::GetFullPath($candidateRoot)
    if (-not $resolved.StartsWith($appsPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe install target: $resolved"
    }
}

function Stop-InstalledProcess {
    param([Parameter(Mandatory)][string]$ValueName)
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    $properties = Get-ItemProperty -LiteralPath $runKey -Name $ValueName `
        -ErrorAction SilentlyContinue
    $property = if ($properties) { $properties.PSObject.Properties[$ValueName] } else { $null }
    $command = if ($property) { [string]$property.Value } else { '' }
    if (-not $command) { return }
    $executable = if ($command -match '^"([^"]+)"') { $Matches[1] } else {
        $command.Split(' ')[0]
    }
    $expected = [IO.Path]::GetFullPath($executable)
    Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).Equals(
            $expected,
            [StringComparison]::OrdinalIgnoreCase
        )
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

New-Item -ItemType Directory -Path $appsRoot -Force | Out-Null
Copy-Item -LiteralPath $sourceRoot -Destination $stagingRoot -Recurse
Stop-InstalledProcess -ValueName 'Codito Agent Daemon'
Stop-InstalledProcess -ValueName 'Codito Agent Tray'

if (Test-Path -LiteralPath $finalRoot) {
    $backupRoot = "$finalRoot.previous-" + (Get-Date -Format 'yyyyMMddHHmmss')
    Move-Item -LiteralPath $finalRoot -Destination $backupRoot
}
Move-Item -LiteralPath $stagingRoot -Destination $finalRoot

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
New-Item -Path $runKey -Force | Out-Null
$daemon = Join-Path $finalRoot 'daemon\codito-agent-daemon.exe'
$tray = Join-Path $finalRoot 'tray\codito-agent-tray.exe'
Set-ItemProperty -LiteralPath $runKey -Name 'Codito Agent Daemon' -Value "`"$daemon`""
Set-ItemProperty -LiteralPath $runKey -Name 'Codito Agent Tray' -Value "`"$tray`" --minimized"
[IO.File]::WriteAllText(
    (Join-Path $productRoot 'current-version'),
    "$version`n",
    [Text.UTF8Encoding]::new($false)
)

if (-not $NoStart) {
    Start-Process -FilePath $daemon -WorkingDirectory (Split-Path $daemon) -WindowStyle Hidden
    Start-Process -FilePath $tray -WorkingDirectory (Split-Path $tray)
}
Write-Output "Codito $version installed for the current Windows user."
