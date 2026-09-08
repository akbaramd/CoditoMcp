[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptDirectory = Split-Path -Parent $PSCommandPath
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory '..\..\..'))
$agentProject = Get-Content -LiteralPath (Join-Path $repositoryRoot 'agent\pyproject.toml') -Raw
if ($agentProject -notmatch 'pyinstaller==6\.22\.2') {
    throw 'agent/pyproject.toml must pin PyInstaller exactly to 6.22.2.'
}
$wixProject = Get-Content -LiteralPath `
    (Join-Path $repositoryRoot 'agent\packaging\wix\Codito.Agent.wixproj') -Raw
if ($wixProject -notmatch 'WixToolset\.Sdk/6\.0\.2') {
    throw 'The WiX SDK must be pinned exactly to 6.0.2.'
}
$wixSource = Get-Content -LiteralPath `
    (Join-Path $repositoryRoot 'agent\packaging\wix\Package.wxs') -Raw
foreach ($required in @(
    'Scope="perUser"',
    'Codito Agent Daemon',
    'Codito Agent Tray',
    'Software\Microsoft\Windows\CurrentVersion\Run'
)) {
    if (-not $wixSource.Contains($required)) {
        throw "Required WiX contract is missing: $required"
    }
}
$packagingSources = Get-ChildItem -LiteralPath (Join-Path $repositoryRoot 'agent\packaging') `
    -File -Recurse | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw }
$forbiddenAcceptanceTokens = @(
    ('Accept' + 'Eula'),
    ('accept' + '-eula')
)
if (($packagingSources -join "`n") -match ('(?i)' + ($forbiddenAcceptanceTokens -join '|'))) {
    throw 'Packaging must never accept the WiX OSMF EULA on the user''s behalf.'
}
Write-Output 'Windows packaging configuration is internally consistent.'
