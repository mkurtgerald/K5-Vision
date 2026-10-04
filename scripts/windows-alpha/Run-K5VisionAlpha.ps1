[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"),
    [ValidateRange(1024,65535)][int]$Port = 8000,
    [string]$PublicRtspSource = "",
    [switch]$ExitAfterPublicTest
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

$launcher = Join-Path $InstallRoot "Start-K5VisionAlpha.ps1"
if (-not (Test-Path -LiteralPath $launcher)) { throw "Run Install-K5VisionAlpha.ps1 first." }

$nativeExitBefore = Get-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
$nativeExitBeforePresent = $null -ne $nativeExitBefore
$nativeExitBeforeValue = if ($nativeExitBeforePresent) { [int]$nativeExitBefore.Value } else { 0 }

if ([string]::IsNullOrWhiteSpace($PublicRtspSource)) {
    & $launcher -Port $Port -ExitAfterPublicTest:$ExitAfterPublicTest
} else {
    & $launcher -Port $Port -PublicRtspSource $PublicRtspSource -ExitAfterPublicTest:$ExitAfterPublicTest
}

$nativeExitAfter = Get-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
if ($null -ne $nativeExitAfter) {
    $nativeExitAfterValue = [int]$nativeExitAfter.Value
    $nativeExitChanged = -not $nativeExitBeforePresent -or $nativeExitAfterValue -ne $nativeExitBeforeValue
    if ($nativeExitChanged -and $nativeExitAfterValue -ne 0) {
        throw "K5 Vision Alpha launcher failed."
    }
}
