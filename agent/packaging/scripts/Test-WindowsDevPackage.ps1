[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ArchivePath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$archiveFullPath = [System.IO.Path]::GetFullPath($ArchivePath)
if (-not (Test-Path -LiteralPath $archiveFullPath -PathType Leaf)) {
    throw "Developer archive does not exist: $archiveFullPath"
}
$outerManifestPath = "$archiveFullPath.sha256"
if (-not (Test-Path -LiteralPath $outerManifestPath -PathType Leaf)) {
    throw "Outer SHA-256 file does not exist: $outerManifestPath"
}
$outerLine = (Get-Content -LiteralPath $outerManifestPath -Raw).Trim()
if ($outerLine -notmatch '^(?<hash>[0-9a-f]{64})  (?<name>[^/\\]+)$') {
    throw 'Outer SHA-256 file is malformed.'
}
if ($Matches.name -ne [System.IO.Path]::GetFileName($archiveFullPath)) {
    throw 'Outer SHA-256 filename does not match the archive.'
}
$actualOuterHash = (Get-FileHash -LiteralPath $archiveFullPath -Algorithm SHA256).
    Hash.ToLowerInvariant()
if ($actualOuterHash -ne $Matches.hash) {
    throw 'Outer SHA-256 verification failed.'
}

Add-Type -AssemblyName System.IO.Compression
$stream = [System.IO.File]::OpenRead($archiveFullPath)
try {
    $archive = [System.IO.Compression.ZipArchive]::new(
        $stream,
        [System.IO.Compression.ZipArchiveMode]::Read,
        $false
    )
    try {
        $entries = @($archive.Entries | Where-Object { $_.Name })
        if ($entries.Count -eq 0 -or $entries.Count -gt 20000) {
            throw "Unexpected ZIP entry count: $($entries.Count)"
        }
        $totalBytes = ($entries | Measure-Object Length -Sum).Sum
        if ($totalBytes -gt 2GB) {
            throw 'The uncompressed developer payload exceeds the 2 GiB safety limit.'
        }
        $entryNames = [string[]]@($entries | ForEach-Object FullName)
        $orderedNames = [string[]]$entryNames.Clone()
        [System.Array]::Sort($orderedNames, [System.StringComparer]::Ordinal)
        if (-not [System.Linq.Enumerable]::SequenceEqual($entryNames, $orderedNames)) {
            throw 'ZIP entries are not in deterministic ordinal order.'
        }
        $uniqueNames = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::OrdinalIgnoreCase
        )
        $expectedRoot = [System.IO.Path]::GetFileNameWithoutExtension($archiveFullPath)
        $entryByRelativePath = @{}
        foreach ($entry in $entries) {
            if (-not $uniqueNames.Add($entry.FullName)) {
                throw "Duplicate case-insensitive ZIP path: $($entry.FullName)"
            }
            if (-not $entry.FullName.StartsWith("$expectedRoot/", [System.StringComparison]::Ordinal)) {
                throw "ZIP entry is outside the expected package root: $($entry.FullName)"
            }
            $relativePath = $entry.FullName.Substring($expectedRoot.Length + 1)
            $segments = $relativePath.Split('/')
            if ($relativePath.Contains('\') -or [System.IO.Path]::IsPathRooted($relativePath) -or
                $segments -contains '..' -or $segments -contains '.') {
                throw "Unsafe ZIP entry path: $($entry.FullName)"
            }
            if ($entry.LastWriteTime.UtcDateTime -ne [datetime]::new(
                1980,
                1,
                1,
                0,
                0,
                0,
                [System.DateTimeKind]::Utc
            )) {
                throw "Non-deterministic ZIP timestamp: $($entry.FullName)"
            }
            $entryByRelativePath[$relativePath] = $entry
        }

        foreach ($requiredPath in @(
            'cli/codito-agent.exe',
            'daemon/codito-agent-daemon.exe',
            'tray/codito-agent-tray.exe',
            'broker/Codito.Broker.exe',
            'SHA256SUMS'
        )) {
            if (-not $entryByRelativePath.ContainsKey($requiredPath)) {
                throw "Required package file is missing: $requiredPath"
            }
        }

        $manifestEntry = $entryByRelativePath['SHA256SUMS']
        $manifestStream = $manifestEntry.Open()
        try {
            $reader = [System.IO.StreamReader]::new(
                $manifestStream,
                [System.Text.UTF8Encoding]::new($false, $true),
                $true,
                4096,
                $true
            )
            try {
                $manifestText = $reader.ReadToEnd()
            }
            finally {
                $reader.Dispose()
            }
        }
        finally {
            $manifestStream.Dispose()
        }

        $manifestPaths = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::Ordinal
        )
        foreach ($line in $manifestText.Split("`n", [System.StringSplitOptions]::RemoveEmptyEntries)) {
            $normalizedLine = $line.TrimEnd("`r")
            if ($normalizedLine -notmatch '^(?<hash>[0-9a-f]{64})  (?<path>.+)$') {
                throw "Malformed inner manifest line: $normalizedLine"
            }
            $expectedHash = $Matches.hash
            $relativePath = $Matches.path
            if (-not $manifestPaths.Add($relativePath)) {
                throw "Duplicate inner manifest path: $relativePath"
            }
            if (-not $entryByRelativePath.ContainsKey($relativePath)) {
                throw "Manifest file is missing from ZIP: $relativePath"
            }
            $payloadStream = $entryByRelativePath[$relativePath].Open()
            try {
                $sha256 = [System.Security.Cryptography.SHA256]::Create()
                try {
                    $actualHash = [System.Convert]::ToHexString(
                        $sha256.ComputeHash($payloadStream)
                    ).ToLowerInvariant()
                }
                finally {
                    $sha256.Dispose()
                }
            }
            finally {
                $payloadStream.Dispose()
            }
            if ($actualHash -ne $expectedHash) {
                throw "Inner SHA-256 verification failed: $relativePath"
            }
        }
        $payloadPaths = @($entryByRelativePath.Keys | Where-Object { $_ -ne 'SHA256SUMS' })
        if ($manifestPaths.Count -ne $payloadPaths.Count) {
            throw 'Inner manifest does not cover every payload file exactly once.'
        }
    }
    finally {
        $archive.Dispose()
    }
}
finally {
    $stream.Dispose()
}

Write-Output "Verified $([System.IO.Path]::GetFileName($archiveFullPath)): $actualOuterHash"
