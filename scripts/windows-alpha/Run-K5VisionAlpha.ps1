[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"),
    [ValidateRange(1024,65535)][int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

$python = Join-Path $InstallRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Run Install-K5VisionAlpha.ps1 first." }

Remove-Item Env:K5_STAGE_ONE_RECORDING_ROOT -ErrorAction SilentlyContinue
Write-Host "Starting K5 Vision Alpha on http://127.0.0.1:$Port"
Write-Host "This bootstrap does not enroll a camera and does not enable recording."
& $python -m k5vision.cli serve --host 127.0.0.1 --port $Port
