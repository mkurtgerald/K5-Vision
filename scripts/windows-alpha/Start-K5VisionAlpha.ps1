[CmdletBinding()]
param([ValidateRange(1024,65535)][int]$Port = 8000)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$versionRecord = Join-Path $PSScriptRoot "gstreamer-version.txt"
if (-not (Test-Path -LiteralPath $python)) { throw "Run Install-K5VisionAlpha.ps1 first." }
if (-not (Test-Path -LiteralPath $versionRecord)) { throw "Installed GStreamer version record is missing." }

$gstreamerVersion = (Get-Content -LiteralPath $versionRecord -Raw).Trim()
if ($gstreamerVersion -notmatch '^1\.28\.\d+$') { throw "Installed GStreamer version record is invalid." }
$gstreamerRoot = Join-Path $env:LOCALAPPDATA "K5RunnerTools\k5-gstreamer\$gstreamerVersion\msvc_x86_64"
$gstCoreReady = (Test-Path -LiteralPath (Join-Path $gstreamerRoot "bin\gstreamer-1.0-0.dll")) -or
    (Test-Path -LiteralPath (Join-Path $gstreamerRoot "bin\libgstreamer-1.0-0.dll"))
if (-not $gstCoreReady) { throw "Reviewed GStreamer runtime is unavailable. Run Test-K5VisionAlpha.ps1." }

$sessionRoot = Join-Path $env:TEMP ("K5VisionAlpha-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $sessionRoot | Out-Null

function New-K5Token {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+","-").Replace("/","_")
}

$writeToken = New-K5Token
$adminToken = New-K5Token
$process = $null

try {
    $env:K5_CONTROL_PLANE_SITE_ID = "alpha-" + [Guid]::NewGuid().ToString("N")
    $env:K5_DEVICE_DB_PATH = Join-Path $sessionRoot "devices.sqlite3"
    $env:K5_USER_DB_PATH = Join-Path $sessionRoot "users.sqlite3"
    $env:K5_CONTROL_PLANE_TOKEN = $writeToken
    $env:K5_CONTROL_PLANE_ADMIN_TOKEN = $adminToken
    $env:K5_GSTREAMER_ROOT = $gstreamerRoot
    $env:GST_REGISTRY_1_0 = Join-Path $sessionRoot "gstreamer-registry.bin"
    Remove-Item Env:K5_STAGE_ONE_RECORDING_ROOT -ErrorAction SilentlyContinue

    Write-Host "Starting K5 Vision Alpha on http://127.0.0.1:$Port"
    Write-Host "Recording is disabled. Temporary K5 state will be removed when this launcher exits."
    $arguments = @("-m","k5vision.cli","serve","--host","127.0.0.1","--port",$Port)
    $process = Start-Process -FilePath $python -ArgumentList $arguments -PassThru -NoNewWindow

    $ready = $false
    foreach ($attempt in 1..30) {
        if ($process.HasExited) { break }
        try {
            $health = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 1
            if ($health.status -eq "ok") { $ready = $true; break }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) { throw "K5 Vision Alpha failed its local health check." }
    Write-Host "K5 Vision Alpha health check PASS."
    Start-Process "http://127.0.0.1:$Port/docs"
    Wait-Process -Id $process.Id
}
finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $process.Id -ErrorAction SilentlyContinue
    }
    foreach ($name in @(
        "K5_CONTROL_PLANE_SITE_ID","K5_DEVICE_DB_PATH","K5_USER_DB_PATH",
        "K5_CONTROL_PLANE_TOKEN","K5_CONTROL_PLANE_ADMIN_TOKEN",
        "K5_GSTREAMER_ROOT","GST_REGISTRY_1_0","K5_STAGE_ONE_RECORDING_ROOT"
    )) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $sessionRoot) {
        Remove-Item -LiteralPath $sessionRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    $writeToken = $null
    $adminToken = $null
}
