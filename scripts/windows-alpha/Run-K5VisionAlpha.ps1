[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"),
    [ValidateRange(1024,65535)][int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

$launcher = Join-Path $InstallRoot "Start-K5VisionAlpha.ps1"
if (-not (Test-Path -LiteralPath $launcher)) { throw "Run Install-K5VisionAlpha.ps1 first." }

& $launcher -Port $Port
if ($LASTEXITCODE -ne 0) { throw "K5 Vision Alpha launcher failed." }
