[CmdletBinding()]
param(
    [ValidateRange(1024,65535)][int]$Port = 8000,
    [string]$PublicRtspSource = "rtsp://9627b0bf2a7b.entrypoint.cloud.wowza.com:1935/app-p5260J38/66abe4b9_stream1",
    [switch]$ExitAfterPublicTest
)

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

$resolveCode = "from k5vision.operator_runtime import resolve_public_test_source_ip; import sys; print(resolve_public_test_source_ip(sys.argv[1]))"
$resolved = @(& $python -c $resolveCode $PublicRtspSource 2>$null)
if ($LASTEXITCODE -ne 0 -or $resolved.Count -ne 1) {
    throw "Public RTSP alpha source failed validation."
}
$publicSourceIp = ([string]$resolved[0]).Trim()
if ([string]::IsNullOrWhiteSpace($publicSourceIp)) { throw "Public RTSP alpha source failed validation." }

try { $publicUri = [Uri]$PublicRtspSource }
catch { throw "Public RTSP alpha source is invalid." }
$rtspPort = if ($publicUri.Port -gt 0) { $publicUri.Port } else { 554 }

$sessionRoot = Join-Path $env:TEMP ("K5VisionAlpha-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $sessionRoot | Out-Null

function New-K5Token {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+","-").Replace("/","_")
}

function ConvertTo-K5Json([hashtable]$Value) {
    return ($Value | ConvertTo-Json -Compress -Depth 5)
}

$writeToken = New-K5Token
$adminToken = New-K5Token
$operatorPassword = New-K5Token
$sessionToken = $null
$bootstrap = $null
$process = $null

try {
    $env:K5_CONTROL_PLANE_SITE_ID = "alpha-" + [Guid]::NewGuid().ToString("N")
    $env:K5_DEVICE_DB_PATH = Join-Path $sessionRoot "devices.sqlite3"
    $env:K5_USER_DB_PATH = Join-Path $sessionRoot "users.sqlite3"
    $env:K5_CONTROL_PLANE_TOKEN = $writeToken
    $env:K5_CONTROL_PLANE_ADMIN_TOKEN = $adminToken
    $env:K5_GSTREAMER_ROOT = $gstreamerRoot
    $gstreamerBin = Join-Path $gstreamerRoot "bin"
    $priorPath = $env:PATH
    $env:PATH = $gstreamerBin + [IO.Path]::PathSeparator + $priorPath
    $env:GST_REGISTRY_1_0 = Join-Path $sessionRoot "gstreamer-registry.bin"
    $env:K5_PUBLIC_TEST_RTSP_SOURCE = $PublicRtspSource
    $env:K5_PUBLIC_TEST_SOURCE_IP = $publicSourceIp
    $env:K5_OPERATOR_STREAM_TOKEN = "public-test"
    Remove-Item Env:K5_OPERATOR_RTP_PAYLOAD_TYPE -ErrorAction SilentlyContinue
    Remove-Item Env:K5_STAGE03_SOURCE -ErrorAction SilentlyContinue
    Remove-Item Env:K5_STAGE03_CAM_CRED -ErrorAction SilentlyContinue
    Remove-Item Env:K5_STAGE_ONE_RECORDING_ROOT -ErrorAction SilentlyContinue

    Write-Host "Starting K5 Vision Alpha public RTSP operator test on http://127.0.0.1:$Port"
    Write-Host "Recording is disabled. Public test media and temporary K5 state are not retained."
    $arguments = @("-m","k5vision.cli","serve","--operator","--host","127.0.0.1","--port",$Port)
    $process = Start-Process -FilePath $python -ArgumentList $arguments -PassThru -NoNewWindow

    $baseUri = "http://127.0.0.1:$Port"
    $ready = $false
    foreach ($attempt in 1..40) {
        if ($process.HasExited) { break }
        try {
            $health = Invoke-RestMethod -Method Get -Uri "$baseUri/api/v1/health" -TimeoutSec 1
            if ($health.status -eq "ok") { $ready = $true; break }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) { throw "K5 Vision Alpha failed its local health check." }
    Write-Host "K5 Vision Alpha health check PASS."

    $adminHeaders = @{ Authorization = "Bearer $adminToken" }
    $bootstrap = Invoke-RestMethod -Method Post -Uri "$baseUri/api/v1/users" -Headers $adminHeaders -ContentType "application/json" -Body (
        ConvertTo-K5Json @{
            username = "alpha_operator"
            display_name = "K5 Alpha Operator"
            role = "operator"
            enabled = $true
        }
    )
    Invoke-RestMethod -Method Post -Uri "$baseUri/api/v1/auth/bootstrap-password" -ContentType "application/json" -Body (
        ConvertTo-K5Json @{
            username = "alpha_operator"
            temporary_credential = $bootstrap.temporary_credential
            new_password = $operatorPassword
        }
    ) | Out-Null
    $login = Invoke-RestMethod -Method Post -Uri "$baseUri/api/v1/auth/login" -ContentType "application/json" -Body (
        ConvertTo-K5Json @{
            username = "alpha_operator"
            password = $operatorPassword
        }
    )
    $sessionToken = $login.session_token
    if ([string]::IsNullOrWhiteSpace($sessionToken)) { throw "K5 alpha operator login failed." }

    $writeHeaders = @{ Authorization = "Bearer $writeToken" }
    $device = Invoke-RestMethod -Method Post -Uri "$baseUri/api/v1/devices" -Headers $writeHeaders -ContentType "application/json" -Body (
        ConvertTo-K5Json @{
            name = "K5 Public RTSP Test"
            host = $publicSourceIp
            management_port = $rtspPort
            kind = "camera"
            protocols = @("rtsp")
            tags = @("alpha-public-test","ephemeral","non-recording")
        }
    )
    if ([string]::IsNullOrWhiteSpace($device.id)) { throw "K5 public test device enrollment failed." }

    Write-Host "Launching the authenticated K5 Windows operator path..."
    $operatorHeaders = @{ Authorization = "Bearer $sessionToken" }
    $receipt = Invoke-RestMethod -Method Post -Uri "$baseUri/api/v1/operator/live" -Headers $operatorHeaders -ContentType "application/json" -TimeoutSec 60 -Body (
        ConvertTo-K5Json @{
            device_id = $device.id
            stream_token = "public-test"
            width = 1280
            height = 720
        }
    )
    if (-not $receipt.completed -or $receipt.delivered_frames -lt 1 -or $receipt.presentations -lt 1) {
        throw "K5 Windows operator public RTSP test did not complete cleanly."
    }

    Write-Host ("K5 public RTSP operator PASS: frames={0}, presentations={1}" -f $receipt.delivered_frames, $receipt.presentations)
    Write-Host "No public-stream recording or retained media was created."
    if ($ExitAfterPublicTest) {
        Write-Host "Exiting after one bounded public RTSP acceptance run."
        return
    }
    Start-Process "$baseUri/docs"
    Write-Host "K5 control plane remains available locally. Close this console to end the alpha session."
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
        "K5_GSTREAMER_ROOT","GST_REGISTRY_1_0","K5_STAGE_ONE_RECORDING_ROOT",
        "K5_PUBLIC_TEST_RTSP_SOURCE","K5_PUBLIC_TEST_SOURCE_IP",
        "K5_OPERATOR_STREAM_TOKEN","K5_OPERATOR_RTP_PAYLOAD_TYPE",
        "K5_STAGE03_SOURCE","K5_STAGE03_CAM_CRED"
    )) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $sessionRoot) {
        Remove-Item -LiteralPath $sessionRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    $sessionToken = $null
    $operatorPassword = $null
    $bootstrap = $null
    $writeToken = $null
    $adminToken = $null
    if ($null -ne $priorPath) { $env:PATH = $priorPath }
    $publicSourceIp = $null
}
