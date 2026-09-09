[CmdletBinding()]
param(
    [string]$PackageRoot,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $PackageRoot) {
    $PackageRoot = $PSScriptRoot
}
if (-not $PackageRoot) {
    throw 'PackageRoot could not be resolved from the installer location.'
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
    -not [Environment]::Is64BitOperatingSystem) {
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
if ($version -notmatch '^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$') {
    throw "Invalid Codito package version: $version"
}

$requiredFiles = @(
    'cli/codito-agent.exe',
    'daemon/codito-agent-daemon.exe',
    'tray/codito-agent-tray.exe',
    'broker/Codito.Broker.exe',
    'daemon/_internal/playwright/driver/node.exe',
    'daemon/_internal/playwright/driver/package/cli.js'
)
$sourcePrefix = $sourceRoot.TrimEnd('\') + '\'
$sourceItem = Get-Item -LiteralPath $sourceRoot -Force
if (($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "The Codito package root is a prohibited reparse point: $sourceRoot"
}
$packageItems = [System.Collections.Generic.List[System.IO.FileSystemInfo]]::new()
$pendingDirectories = [System.Collections.Generic.Stack[System.IO.DirectoryInfo]]::new()
$pendingDirectories.Push($sourceItem)
while ($pendingDirectories.Count -gt 0) {
    $directory = $pendingDirectories.Pop()
    foreach ($item in Get-ChildItem -LiteralPath $directory.FullName -Force) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The Codito package contains a prohibited reparse point: $($item.FullName)"
        }
        $packageItems.Add($item)
        if ($item.PSIsContainer) {
            $pendingDirectories.Push($item)
        }
    }
}

$physicalFiles = [System.Collections.Generic.Dictionary[string,string]]::new(
    [StringComparer]::Ordinal
)
foreach ($file in @($packageItems | Where-Object { -not $_.PSIsContainer })) {
    $filePath = [IO.Path]::GetFullPath($file.FullName)
    if (-not $filePath.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Package file is outside the package root: $filePath"
    }
    $relative = $filePath.Substring($sourcePrefix.Length).Replace('\', '/')
    if ($relative -ne 'SHA256SUMS') {
        $physicalFiles.Add($relative, $filePath)
    }
}

$manifestEntries = [System.Collections.Generic.Dictionary[string,string]]::new(
    [StringComparer]::Ordinal
)
$manifestPathsIgnoreCase = [System.Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)
foreach ($line in Get-Content -LiteralPath $manifestPath) {
    if (-not $line) { continue }
    if ($line -notmatch '^([a-f0-9]{64})  ([^\\]+)$') {
        throw 'SHA256SUMS contains an invalid entry.'
    }
    $relative = $Matches[2]
    $segments = $relative.Split('/')
    if ([IO.Path]::IsPathRooted($relative) -or $segments -contains '..' -or
        $segments -contains '.' -or $segments -contains '') {
        throw "Unsafe manifest path: $relative"
    }
    if (-not $manifestPathsIgnoreCase.Add($relative)) {
        throw "SHA256SUMS contains a duplicate path: $relative"
    }
    $manifestEntries.Add($relative, $Matches[1])
}
foreach ($relative in $requiredFiles) {
    if (-not $manifestEntries.ContainsKey($relative)) {
        throw "Package manifest omits required file: $relative"
    }
}

$chromeExecutables = @($physicalFiles.Keys | Where-Object {
    [IO.Path]::GetFileName($_).Equals('chrome.exe', [StringComparison]::OrdinalIgnoreCase)
})
if ($chromeExecutables.Count -ne 1 -or
    $chromeExecutables[0] -notmatch '^browsers/chromium-(?<revision>\d+)/chrome-win64/chrome\.exe$') {
    throw 'The package must contain exactly one pinned Chromium executable at the expected path.'
}
$chromiumRevision = $Matches['revision']
$browserPrefix = "browsers/chromium-$chromiumRevision"
$browserRootPrefix = (Join-Path $sourceRoot 'browsers').TrimEnd('\') + '\'
$browserItems = @($packageItems | Where-Object {
    $_.FullName.StartsWith($browserRootPrefix, [StringComparison]::OrdinalIgnoreCase)
})
foreach ($item in $browserItems) {
    $browserItemPath = [IO.Path]::GetFullPath($item.FullName)
    $browserRelative = $browserItemPath.Substring($sourcePrefix.Length).Replace('\', '/')
    if ($browserRelative -ne $browserPrefix -and
        -not $browserRelative.StartsWith("$browserPrefix/", [StringComparison]::Ordinal)) {
        throw "Package browser entry is outside the pinned Chromium revision: $browserRelative"
    }
}

if ($manifestEntries.Count -ne $physicalFiles.Count) {
    throw ('SHA256SUMS does not describe every package file exactly once ' +
        "($($manifestEntries.Count) entries for $($physicalFiles.Count) files).")
}
foreach ($relative in $physicalFiles.Keys) {
    if (-not $manifestEntries.ContainsKey($relative)) {
        throw "Package manifest omits physical file: $relative"
    }
}
foreach ($entry in $manifestEntries.GetEnumerator()) {
    if (-not $physicalFiles.ContainsKey($entry.Key)) {
        throw "Package manifest names a file that is not present: $($entry.Key)"
    }
    $candidate = [IO.Path]::GetFullPath((Join-Path $sourceRoot $entry.Key))
    if (-not $candidate.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or
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
$registration = Start-Process -FilePath $tray -ArgumentList '--register-activation' `
    -WorkingDirectory (Split-Path $tray) -WindowStyle Hidden -Wait -PassThru
if ($registration.ExitCode -ne 0) {
    throw 'Codito notification activation registration failed. No approvals were granted.'
}
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
