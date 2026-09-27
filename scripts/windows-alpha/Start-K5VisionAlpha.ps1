[CmdletBinding()]
param([ValidateRange(1024,65535)][int]$Port = 8000)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Run Install-K5VisionAlpha.ps1 first." }

$sessionRoot = Join-Path $env:TEMP ("K5VisionAlpha-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $sessionRoot | Out-Null
$tokenBytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($tokenBytes) } finally { $rng.Dispose() }
$token = [Convert]::ToBase64String($tokenBytes).TrimEnd("=").Replace("+","-").Replace("/","_")

try {
    $env:K5_CONTROL_PLANE_SITE_ID = "alpha-" + [Guid]::NewGuid().ToString("N")
    $env:K5_DEVICE_DB_PATH = Join-Path $sessionRoot "devices.sqlite3"
    $env:K5_USER_DB_PATH = Join-Path $sessionRoot "users.sqlite3"
    $env:K5_CONTROL_PLANE_TOKEN = $token
    Remove-Item Env:K5_STAGE_ONE_RECORDING_ROOT -ErrorAction SilentlyContinue
    Write-Host "Starting K5 Vision Alpha control plane on http://127.0.0.1:$Port"
    Write-Host "This bootstrap does not contact a camera and does not enable recording."
    & $python -m k5vision.cli serve --host 127.0.0.1 --port $Port
}
finally {
    foreach ($name in @("K5_CONTROL_PLANE_SITE_ID","K5_DEVICE_DB_PATH","K5_USER_DB_PATH","K5_CONTROL_PLANE_TOKEN","K5_STAGE_ONE_RECORDING_ROOT")) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $sessionRoot) {
        Remove-Item -LiteralPath $sessionRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    $token = $null
}
