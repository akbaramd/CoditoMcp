[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '0.3.5',
    [string]$OutputDirectory,
    [long]$SourceDateEpoch = 946684800,
    [switch]$Release
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptDirectory = Split-Path -Parent $PSCommandPath
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory '..\..\..'))
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repositoryRoot 'installer-output'
}
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$repositoryPrefix = $repositoryRoot.TrimEnd('\') + '\'
if (-not $outputRoot.StartsWith($repositoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDirectory must remain inside the repository: $outputRoot"
}
$isWindowsPlatform = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
$isX64 = [Environment]::Is64BitOperatingSystem
if (-not $isWindowsPlatform -or -not $isX64) {
    throw 'Codito Windows packages must be built on Windows x64.'
}

function Reset-RepositoryDirectory {
    param([Parameter(Mandatory)][string]$Path)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($repositoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to reset a directory outside the repository: $fullPath"
    }
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
    New-Item -ItemType Directory -Path $fullPath | Out-Null
}

function Copy-DirectoryContents {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Expected build output is missing: $Source"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Destination -Recurse -Force
}

function Get-SafeRelativePath {
    param(
        [Parameter(Mandatory)][string]$BasePath,
        [Parameter(Mandatory)][string]$ChildPath
    )
    $baseFullPath = [System.IO.Path]::GetFullPath($BasePath).TrimEnd('\') + '\'
    $childFullPath = [System.IO.Path]::GetFullPath($ChildPath)
    if (-not $childFullPath.StartsWith(
        $baseFullPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Path is outside the expected package root: $childFullPath"
    }
    return $childFullPath.Substring($baseFullPath.Length).Replace('\', '/')
}

$pythonExecutable = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
$pyInstallerExecutable = Join-Path $repositoryRoot '.venv\Scripts\pyinstaller.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf) -or
    -not (Test-Path -LiteralPath $pyInstallerExecutable -PathType Leaf)) {
    throw ('Run `uv sync --frozen --package codito-agent --extra ui --extra package` ' +
        'before building the Windows package.')
}
$pyInstallerVersion = (& $pyInstallerExecutable --version).Trim()
if ($LASTEXITCODE -ne 0 -or $pyInstallerVersion -ne '6.22.2') {
    throw "PyInstaller 6.22.2 is required; found '$pyInstallerVersion'."
}

$buildRoot = Join-Path $outputRoot 'build'
$distRoot = Join-Path $outputRoot 'dist'
$playwrightBrowserRoot = Join-Path $outputRoot 'playwright-browser'
$packageName = if ($Release) { "Codito-$Version-win-x64" } else {
    "Codito-$Version-win-x64-dev"
}
$stageRoot = Join-Path (Join-Path $outputRoot 'staging') $packageName
Reset-RepositoryDirectory -Path $buildRoot
Reset-RepositoryDirectory -Path $distRoot
Reset-RepositoryDirectory -Path $stageRoot
Reset-RepositoryDirectory -Path $playwrightBrowserRoot

$versionParts = $Version.Split('.')
$versionTuple = "$($versionParts[0]), $($versionParts[1]), $($versionParts[2]), 0"
$versionFile = Join-Path $buildRoot 'version-info.txt'
$versionTemplate = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($versionTuple), prodvers=($versionTuple),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Codito'),
      StringStruct('FileDescription', 'Codito Windows Agent'),
      StringStruct('FileVersion', '$Version.0'),
      StringStruct('InternalName', 'Codito'),
      StringStruct('LegalCopyright', 'Copyright Codito contributors'),
      StringStruct('OriginalFilename', 'codito-agent.exe'),
      StringStruct('ProductName', 'Codito Windows Agent'),
      StringStruct('ProductVersion', '$Version.0')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"@
[System.IO.File]::WriteAllText(
    $versionFile,
    $versionTemplate,
    [System.Text.UTF8Encoding]::new($false)
)

$previousSourceDateEpoch = $env:SOURCE_DATE_EPOCH
$previousPythonHashSeed = $env:PYTHONHASHSEED
$previousVersionFile = $env:CODITO_VERSION_FILE
$previousPlaywrightBrowserPath = $env:PLAYWRIGHT_BROWSERS_PATH
$previousPlaywrightNodePath = $env:PLAYWRIGHT_NODEJS_PATH
$previousPlaywrightDownloadHost = $env:PLAYWRIGHT_DOWNLOAD_HOST
$previousPlaywrightChromiumDownloadHost = $env:PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST
$previousNodeOptions = $env:NODE_OPTIONS
$previousNodeTlsRejectUnauthorized = $env:NODE_TLS_REJECT_UNAUTHORIZED
$previousNodeExtraCaCerts = $env:NODE_EXTRA_CA_CERTS
try {
    $env:SOURCE_DATE_EPOCH = [string]$SourceDateEpoch
    $env:PYTHONHASHSEED = '0'
    $env:CODITO_VERSION_FILE = $versionFile
    $env:PLAYWRIGHT_BROWSERS_PATH = $playwrightBrowserRoot
    $env:PLAYWRIGHT_NODEJS_PATH = $null
    $env:PLAYWRIGHT_DOWNLOAD_HOST = $null
    $env:PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST = $null
    $env:NODE_OPTIONS = $null
    $env:NODE_TLS_REJECT_UNAUTHORIZED = $null
    $env:NODE_EXTRA_CA_CERTS = $null
    & $pythonExecutable -m playwright install chromium --no-shell
    if ($LASTEXITCODE -ne 0) {
        throw 'Playwright Chromium installation failed.'
    }
    $chromiumRevision = (& $pythonExecutable -c `
        "import json,pathlib,playwright; data=json.loads((pathlib.Path(playwright.__file__).parent/'driver/package/browsers.json').read_text(encoding='utf-8')); print(next(item['revision'] for item in data['browsers'] if item['name']=='chromium'))").Trim()
    if ($LASTEXITCODE -ne 0 -or $chromiumRevision -notmatch '^\d+$') {
        throw "Could not resolve the pinned Playwright Chromium revision: '$chromiumRevision'."
    }
    $playwrightChromiumRoot = Join-Path $playwrightBrowserRoot "chromium-$chromiumRevision"
    $expectedChromiumExecutable = Join-Path $playwrightChromiumRoot 'chrome-win64\chrome.exe'
    $downloadedChromiumExecutables = @(Get-ChildItem -LiteralPath $playwrightBrowserRoot `
        -Filter 'chrome.exe' -File -Recurse -ErrorAction SilentlyContinue)
    if ($downloadedChromiumExecutables.Count -ne 1 -or
        -not $downloadedChromiumExecutables[0].FullName.Equals(
            $expectedChromiumExecutable,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw ('Expected exactly one Chrome executable for pinned Playwright Chromium revision ' +
            "$chromiumRevision at $expectedChromiumExecutable; found " +
            "$($downloadedChromiumExecutables.Count).")
    }
    $specifications = @(
        'codito-agent.spec',
        'codito-agent-daemon.spec',
        'codito-agent-tray.spec'
    )
    foreach ($specification in $specifications) {
        & $pyInstallerExecutable `
            --noconfirm `
            --clean `
            --distpath $distRoot `
            --workpath $buildRoot `
            (Join-Path $repositoryRoot "agent\packaging\pyinstaller\$specification")
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller failed for $specification."
        }
    }
}
finally {
    $env:SOURCE_DATE_EPOCH = $previousSourceDateEpoch
    $env:PYTHONHASHSEED = $previousPythonHashSeed
    $env:CODITO_VERSION_FILE = $previousVersionFile
    $env:PLAYWRIGHT_BROWSERS_PATH = $previousPlaywrightBrowserPath
    $env:PLAYWRIGHT_NODEJS_PATH = $previousPlaywrightNodePath
    $env:PLAYWRIGHT_DOWNLOAD_HOST = $previousPlaywrightDownloadHost
    $env:PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST = $previousPlaywrightChromiumDownloadHost
    $env:NODE_OPTIONS = $previousNodeOptions
    $env:NODE_TLS_REJECT_UNAUTHORIZED = $previousNodeTlsRejectUnauthorized
    $env:NODE_EXTRA_CA_CERTS = $previousNodeExtraCaCerts
}

$brokerPublish = Join-Path $buildRoot 'broker-publish'
New-Item -ItemType Directory -Path $brokerPublish -Force | Out-Null
& dotnet publish `
    (Join-Path $repositoryRoot 'agent\broker\src\Codito.Broker\Codito.Broker.csproj') `
    --configuration Release `
    --maxcpucount:1 `
    --runtime win-x64 `
    --self-contained true `
    --output $brokerPublish `
    -p:PublishSingleFile=true `
    -p:PublishTrimmed=false `
    -p:DebugSymbols=false `
    -p:DebugType=None `
    -p:ContinuousIntegrationBuild=true `
    -p:Deterministic=true
if ($LASTEXITCODE -ne 0) {
    throw 'The self-contained broker publish failed.'
}

Copy-DirectoryContents -Source (Join-Path $distRoot 'codito-agent') `
    -Destination (Join-Path $stageRoot 'cli')
Copy-DirectoryContents -Source (Join-Path $distRoot 'codito-agent-daemon') `
    -Destination (Join-Path $stageRoot 'daemon')
Copy-DirectoryContents -Source (Join-Path $distRoot 'codito-agent-tray') `
    -Destination (Join-Path $stageRoot 'tray')
$browserStageRoot = Join-Path $stageRoot 'browsers'
New-Item -ItemType Directory -Path $browserStageRoot -Force | Out-Null
Copy-DirectoryContents -Source $playwrightChromiumRoot `
    -Destination (Join-Path $browserStageRoot (Split-Path -Leaf $playwrightChromiumRoot))
$brokerStage = Join-Path $stageRoot 'broker'
New-Item -ItemType Directory -Path $brokerStage | Out-Null
Copy-Item -LiteralPath (Join-Path $brokerPublish 'Codito.Broker.exe') -Destination $brokerStage
Copy-Item -LiteralPath (Join-Path $repositoryRoot 'agent\packaging\config.example.toml') `
    -Destination (Join-Path $stageRoot 'config.example.toml')
Copy-Item -LiteralPath (Join-Path $repositoryRoot 'agent\packaging\DEV-README.txt') `
    -Destination (Join-Path $stageRoot 'README.txt')
Copy-Item -LiteralPath (Join-Path $repositoryRoot 'agent\packaging\scripts\Install-WindowsDevPackage.ps1') `
    -Destination (Join-Path $stageRoot 'install-windows.ps1')
Copy-Item -LiteralPath (Join-Path $repositoryRoot 'LICENSE') -Destination $stageRoot
[System.IO.File]::WriteAllText(
    (Join-Path $stageRoot 'VERSION'),
    "$Version`n",
    [System.Text.UTF8Encoding]::new($false)
)

& (Join-Path $stageRoot 'cli\codito-agent.exe') --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Packaged CLI smoke test failed.'
}
& (Join-Path $stageRoot 'daemon\codito-agent-daemon.exe') --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Packaged daemon smoke test failed.'
}
$chromiumExecutables = @(Get-ChildItem -LiteralPath (Join-Path $stageRoot 'browsers') `
    -Filter 'chrome.exe' -File -Recurse)
if ($chromiumExecutables.Count -ne 1) {
    throw "Expected exactly one packaged Playwright Chromium executable; found $($chromiumExecutables.Count)."
}
$playwrightDriverPaths = @(
    'daemon\_internal\playwright\driver\node.exe',
    'daemon\_internal\playwright\driver\package\cli.js'
)
foreach ($relativePath in $playwrightDriverPaths) {
    if (-not (Test-Path -LiteralPath (Join-Path $stageRoot $relativePath) -PathType Leaf)) {
        throw "Packaged Playwright driver file is missing: $relativePath"
    }
}
& (Join-Path $stageRoot 'daemon\codito-agent-daemon.exe') --packaged-browser-smoke
if ($LASTEXITCODE -ne 0) {
    throw 'Packaged Playwright browser smoke test failed.'
}

$brokerInput = '{"version":1,"operation":"probe"}'
$brokerResponse = $brokerInput | & (Join-Path $brokerStage 'Codito.Broker.exe') --json |
    ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $brokerResponse.version -ne 1 -or
    -not $brokerResponse.capabilities.job_object) {
    throw 'Packaged broker capability smoke test failed.'
}

$manifestPath = Join-Path $stageRoot 'SHA256SUMS'
$manifestLines = [System.Collections.Generic.List[string]]::new()
$payloadRelativePaths = [string[]]@(Get-ChildItem -LiteralPath $stageRoot -File -Recurse |
    Where-Object { $_.FullName -ne $manifestPath } |
    ForEach-Object {
        Get-SafeRelativePath -BasePath $stageRoot -ChildPath $_.FullName
    })
[System.Array]::Sort($payloadRelativePaths, [System.StringComparer]::Ordinal)
foreach ($relativePath in $payloadRelativePaths) {
    $fullPath = Join-Path $stageRoot $relativePath
    $hash = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifestLines.Add("$hash  $relativePath")
}
[System.IO.File]::WriteAllLines(
    $manifestPath,
    $manifestLines,
    [System.Text.UTF8Encoding]::new($false)
)

$zipPath = Join-Path $outputRoot "$packageName.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Add-Type -AssemblyName System.IO.Compression
$zipStream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::CreateNew)
try {
    $archive = [System.IO.Compression.ZipArchive]::new(
        $zipStream,
        [System.IO.Compression.ZipArchiveMode]::Create,
        $false
    )
    try {
        $fixedTimestamp = [System.DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [System.TimeSpan]::Zero)
        $archiveRelativePaths = [string[]]@($payloadRelativePaths + 'SHA256SUMS')
        [System.Array]::Sort($archiveRelativePaths, [System.StringComparer]::Ordinal)
        foreach ($relativePath in $archiveRelativePaths) {
            $entry = $archive.CreateEntry(
                "$packageName/$relativePath",
                [System.IO.Compression.CompressionLevel]::Optimal
            )
            $entry.LastWriteTime = $fixedTimestamp
            $entryStream = $entry.Open()
            $sourceStream = [System.IO.File]::OpenRead((Join-Path $stageRoot $relativePath))
            try {
                $sourceStream.CopyTo($entryStream)
            }
            finally {
                $sourceStream.Dispose()
                $entryStream.Dispose()
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}
finally {
    $zipStream.Dispose()
}

$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$zipHashPath = "$zipPath.sha256"
[System.IO.File]::WriteAllText(
    $zipHashPath,
    "$zipHash  $([System.IO.Path]::GetFileName($zipPath))`n",
    [System.Text.UTF8Encoding]::new($false)
)

& (Join-Path $repositoryRoot 'agent\packaging\scripts\Test-WindowsDevPackage.ps1') `
    -ArchivePath $zipPath
if ($LASTEXITCODE -ne 0) {
    throw 'The completed developer ZIP failed integrity verification.'
}

Write-Output $zipPath
Write-Output $zipHashPath
