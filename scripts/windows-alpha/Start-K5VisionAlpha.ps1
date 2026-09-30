[CmdletBinding()]
param(
    [ValidateRange(1024,65535)][int]$Port = 8000,
    [string]$PublicRtspSource = "",
    [switch]$ExitAfterPublicTest
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

$MediaMtxVersion = "1.21.1"
$MediaMtxArchiveSha256 = "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23"
$MediaMtxArchiveUri = "https://github.com/bluenviron/mediamtx/releases/download/v$MediaMtxVersion/mediamtx_v$MediaMtxVersion" + "_windows_amd64.zip"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$versionRecord = Join-Path $PSScriptRoot "gstreamer-version.txt"
if (-not (Test-Path -LiteralPath $python)) { throw "Run Install-K5VisionAlpha.ps1 first." }
if (-not (Test-Path -LiteralPath $versionRecord)) { throw "Installed GStreamer version record is missing." }

$gstreamerVersion = (Get-Content -LiteralPath $versionRecord -Raw).Trim()
if ($gstreamerVersion -notmatch '^1\.28\.\d+$') { throw "Installed GStreamer version record is invalid." }
$gstreamerRoot = Join-Path $env:LOCALAPPDATA "K5RunnerTools\k5-gstreamer\$gstreamerVersion\msvc_x86_64"
$gstreamerBin = Join-Path $gstreamerRoot "bin"
$gstLaunch = Join-Path $gstreamerBin "gst-launch-1.0.exe"
$gstInspect = Join-Path $gstreamerBin "gst-inspect-1.0.exe"
$gstCoreReady = (Test-Path -LiteralPath (Join-Path $gstreamerBin "gstreamer-1.0-0.dll")) -or
    (Test-Path -LiteralPath (Join-Path $gstreamerBin "libgstreamer-1.0-0.dll"))
if (-not $gstCoreReady -or -not (Test-Path -LiteralPath $gstLaunch) -or -not (Test-Path -LiteralPath $gstInspect)) {
    throw "Reviewed GStreamer runtime is unavailable. Run Test-K5VisionAlpha.ps1."
}

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

function Test-K5TcpListener([string]$HostName, [int]$TargetPort) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect($HostName, $TargetPort, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(250)) { return $false }
        $client.EndConnect($result)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-K5GStreamerElement([string]$Name) {
    & $gstInspect $Name *> $null
    return $LASTEXITCODE -eq 0
}

function Get-K5MediaMtx {
    $root = Join-Path $env:LOCALAPPDATA "K5RunnerTools\mediamtx\$MediaMtxVersion"
    $exe = Join-Path $root "mediamtx.exe"
    $archiveRecord = Join-Path $root "k5-archive.sha256"
    $exeRecord = Join-Path $root "k5-exe.sha256"
    $ready = $false

    if ((Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath $archiveRecord) -and (Test-Path -LiteralPath $exeRecord)) {
        $recordedArchive = (Get-Content -LiteralPath $archiveRecord -Raw).Trim().ToLowerInvariant()
        $recordedExe = (Get-Content -LiteralPath $exeRecord -Raw).Trim().ToLowerInvariant()
        $actualExe = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
        $ready = $recordedArchive -eq $MediaMtxArchiveSha256 -and $recordedExe -eq $actualExe
    }

    if (-not $ready) {
        if (Test-Path -LiteralPath $root) {
            Remove-Item -LiteralPath $root -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $root | Out-Null
        $archive = Join-Path $sessionRoot "mediamtx.zip"
        $priorProgress = $ProgressPreference
        try {
            $ProgressPreference = "SilentlyContinue"
            Write-Host "Provisioning pinned local RTSP test server..."
            Invoke-WebRequest -Uri $MediaMtxArchiveUri -OutFile $archive -UseBasicParsing
        } finally {
            $ProgressPreference = $priorProgress
        }
        $actualArchive = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualArchive -ne $MediaMtxArchiveSha256) {
            throw "MediaMTX archive failed pinned SHA-256 verification."
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $root -Force
        if (-not (Test-Path -LiteralPath $exe)) {
            throw "Pinned MediaMTX archive did not contain the Windows executable."
        }
        $actualExe = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
        [IO.File]::WriteAllText($archiveRecord, $MediaMtxArchiveSha256)
        [IO.File]::WriteAllText($exeRecord, $actualExe)
    }

    return $exe
}

function Start-K5SyntheticSource {
    foreach ($element in @("videotestsrc","videoconvert","x264enc","h264parse","rtspclientsink","rtspsrc","queue","identity","fakesink")) {
        if (-not (Test-K5GStreamerElement $element)) {
            throw "Reviewed GStreamer runtime is missing required synthetic test element: $element"
        }
    }

    $mediaMtx = Get-K5MediaMtx
    $configPath = Join-Path $sessionRoot "mediamtx.yml"
    $config = @"
logLevel: warn
api: false
metrics: false
pprof: false
playback: false
rtsp: true
rtspTransports: [tcp]
rtspAddress: 127.0.0.1:8554
rtmp: false
hls: false
webrtc: false
srt: false
moq: false
paths:
  k5synthetic:
"@
    [IO.File]::WriteAllText($configPath, $config)

    $mediaMtxVersion = @(& $mediaMtx --version 2>&1)
    if ($LASTEXITCODE -ne 0 -or -not (($mediaMtxVersion -join " ").Contains($MediaMtxVersion))) {
        foreach ($line in $mediaMtxVersion) { Write-Host ("mediamtx-version: " + $line) }
        throw "Pinned MediaMTX executable failed its version probe."
    }

    $validation = @(& $mediaMtx --validate-conf $configPath 2>&1)
    if ($LASTEXITCODE -ne 0) {
        foreach ($line in $validation) { Write-Host ("mediamtx-validate: " + $line) }
        throw "Local synthetic RTSP MediaMTX configuration is invalid."
    }
    foreach ($line in $validation) {
        if (-not [string]::IsNullOrWhiteSpace([string]$line)) {
            Write-Host ("mediamtx-validate: " + $line)
        }
    }

    if (Test-K5TcpListener "127.0.0.1" 8554) {
        throw "Local synthetic RTSP port 8554 is already in use."
    }

    Write-Host "Starting local MediaMTX RTSP server..."
    $server = Start-Process -FilePath $mediaMtx -ArgumentList @($configPath) -PassThru -NoNewWindow
    $serverReady = $false
    foreach ($attempt in 1..40) {
        if ($server.HasExited) { break }
        if (Test-K5TcpListener "127.0.0.1" 8554) {
            $serverReady = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $serverReady) {
        $serverState = if ($server.HasExited) { "exited:$($server.ExitCode)" } else { "running" }
        Write-Host ("Synthetic RTSP server diagnostics: state={0}" -f $serverState)
        if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
        throw "Local synthetic RTSP server failed to start."
    }

    $source = "rtsp://127.0.0.1:8554/k5synthetic"
    $publisherArgs = @(
        "-q",
        "videotestsrc","is-live=true","pattern=smpte",
        "!","video/x-raw,width=1280,height=720,format=I420,framerate=15/1",
        "!","videoconvert",
        "!","x264enc","speed-preset=ultrafast","tune=zerolatency","bitrate=2000","key-int-max=30",
        "!","video/x-h264,profile=baseline",
        "!","h264parse","config-interval=1",
        "!","rtspclientsink","protocols=tcp","location=$source"
    )
    $publisherOut = Join-Path $sessionRoot "publisher.stdout.log"
    $publisherErr = Join-Path $sessionRoot "publisher.stderr.log"
    $publisher = Start-Process -FilePath $gstLaunch -ArgumentList $publisherArgs -PassThru -WindowStyle Hidden -RedirectStandardOutput $publisherOut -RedirectStandardError $publisherErr

    $probeArgs = @(
        "-q",
        "rtspsrc","location=$source","protocols=tcp","latency=50","tcp-timeout=2000000","teardown-timeout=0",
        "!","queue",
        "!","fakesink","num-buffers=1","sync=false","async=false"
    )
    $sourceReady = $false
    $probeExitCode = "not-run"
    foreach ($attempt in 1..20) {
        if ($publisher.HasExited -or $server.HasExited) { break }
        $probeOut = Join-Path $sessionRoot ("probe-{0}.stdout.log" -f $attempt)
        $probeErr = Join-Path $sessionRoot ("probe-{0}.stderr.log" -f $attempt)
        $probe = Start-Process -FilePath $gstLaunch -ArgumentList $probeArgs -PassThru -WindowStyle Hidden -RedirectStandardOutput $probeOut -RedirectStandardError $probeErr
        if (-not $probe.WaitForExit(3000)) {
            Stop-Process -Id $probe.Id -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $probe.Id -ErrorAction SilentlyContinue
            $probeExitCode = -1
        } else {
            $probeExitCode = $probe.ExitCode
            if ($probeExitCode -eq 0) {
                $sourceReady = $true
                break
            }
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $sourceReady) {
        $publisherState = if ($publisher.HasExited) { "exited:$($publisher.ExitCode)" } else { "running" }
        $serverState = if ($server.HasExited) { "exited:$($server.ExitCode)" } else { "running" }
        Write-Host ("Synthetic RTSP diagnostics: server={0}, publisher={1}, lastProbe={2}" -f $serverState, $publisherState, $probeExitCode)
        if (Test-Path -LiteralPath $publisherErr) {
            $publisherTail = @(Get-Content -LiteralPath $publisherErr -Tail 8 -ErrorAction SilentlyContinue)
            foreach ($line in $publisherTail) { Write-Host ("publisher: " + $line) }
        }
        Stop-Process -Id $publisher.Id -Force -ErrorAction SilentlyContinue
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        throw "Local synthetic RTSP source failed its bounded readiness probe."
    }

    Write-Host "Local synthetic RTSP source PASS."
    return @{
        Uri = $source
        Ip = "127.0.0.1"
        Port = 8554
        StreamToken = "local-test"
        DeviceName = "K5 Local Synthetic Test"
        Tags = @("alpha-local-synthetic","ephemeral","non-recording")
        ServerProcess = $server
        PublisherProcess = $publisher
    }
}

$writeToken = New-K5Token
$adminToken = New-K5Token
$operatorPassword = New-K5Token
$sessionToken = $null
$bootstrap = $null
$process = $null
$mediaMtxProcess = $null
$publisherProcess = $null
$priorPath = [string]$env:PATH
$priorGioProxyResolver = [Environment]::GetEnvironmentVariable("GIO_USE_PROXY_RESOLVER", "Process")
$priorGioModuleDir = [Environment]::GetEnvironmentVariable("GIO_MODULE_DIR", "Process")
$priorNoProxy = [Environment]::GetEnvironmentVariable("no_proxy", "Process")
$priorNoProxyUpper = [Environment]::GetEnvironmentVariable("NO_PROXY", "Process")
$publicSourceIp = $null

try {
    $gioModuleDir = Join-Path $sessionRoot "gio-modules"
    New-Item -ItemType Directory -Force -Path $gioModuleDir | Out-Null
    $env:GIO_MODULE_DIR = $gioModuleDir
    $env:GIO_USE_PROXY_RESOLVER = "dummy"
    $env:no_proxy = "*"
    $env:NO_PROXY = "*"
    $env:PATH = if ([string]::IsNullOrWhiteSpace($priorPath)) {
        $gstreamerBin
    } else {
        $gstreamerBin + [IO.Path]::PathSeparator + $priorPath
    }

    $sourceUri = $null
    $sourceIp = $null
    $rtspPort = $null
    $streamToken = $null
    $deviceName = $null
    $deviceTags = $null

    if ([string]::IsNullOrWhiteSpace($PublicRtspSource)) {
        $synthetic = Start-K5SyntheticSource
        $sourceUri = $synthetic.Uri
        $sourceIp = $synthetic.Ip
        $rtspPort = $synthetic.Port
        $streamToken = $synthetic.StreamToken
        $deviceName = $synthetic.DeviceName
        $deviceTags = $synthetic.Tags
        $mediaMtxProcess = $synthetic.ServerProcess
        $publisherProcess = $synthetic.PublisherProcess
        $env:K5_LOCAL_TEST_RTSP_SOURCE = $sourceUri
        Remove-Item Env:K5_PUBLIC_TEST_RTSP_SOURCE -ErrorAction SilentlyContinue
        Remove-Item Env:K5_PUBLIC_TEST_SOURCE_IP -ErrorAction SilentlyContinue
        Write-Host "Starting K5 Vision Alpha local synthetic operator test on http://127.0.0.1:$Port"
    } else {
        $resolveCode = "from k5vision.operator_runtime import resolve_public_test_source_ip; import sys; print(resolve_public_test_source_ip(sys.argv[1]))"
        $resolved = @(& $python -c $resolveCode $PublicRtspSource 2>$null)
        if ($LASTEXITCODE -ne 0 -or $resolved.Count -ne 1) {
            throw "Public RTSP alpha source failed validation."
        }
        $publicSourceIp = ([string]$resolved[0]).Trim()
        if ([string]::IsNullOrWhiteSpace($publicSourceIp)) {
            throw "Public RTSP alpha source failed validation."
        }
        try { $publicUri = [Uri]$PublicRtspSource }
        catch { throw "Public RTSP alpha source is invalid." }
        $sourceUri = $PublicRtspSource
        $sourceIp = $publicSourceIp
        $rtspPort = if ($publicUri.Port -gt 0) { $publicUri.Port } else { 554 }
        $streamToken = "public-test"
        $deviceName = "K5 Public RTSP Test"
        $deviceTags = @("alpha-public-test","ephemeral","non-recording")
        $env:K5_PUBLIC_TEST_RTSP_SOURCE = $PublicRtspSource
        $env:K5_PUBLIC_TEST_SOURCE_IP = $publicSourceIp
        Remove-Item Env:K5_LOCAL_TEST_RTSP_SOURCE -ErrorAction SilentlyContinue
        Write-Host "Starting K5 Vision Alpha public RTSP operator test on http://127.0.0.1:$Port"
    }

    $env:K5_CONTROL_PLANE_SITE_ID = "alpha-" + [Guid]::NewGuid().ToString("N")
    $env:K5_DEVICE_DB_PATH = Join-Path $sessionRoot "devices.sqlite3"
    $env:K5_USER_DB_PATH = Join-Path $sessionRoot "users.sqlite3"
    $env:K5_CONTROL_PLANE_TOKEN = $writeToken
    $env:K5_CONTROL_PLANE_ADMIN_TOKEN = $adminToken
    $env:K5_GSTREAMER_ROOT = $gstreamerRoot
    $env:GST_REGISTRY_1_0 = Join-Path $sessionRoot "gstreamer-registry.bin"
    $env:K5_OPERATOR_STREAM_TOKEN = $streamToken
    Remove-Item Env:K5_OPERATOR_RTP_PAYLOAD_TYPE -ErrorAction SilentlyContinue
    Remove-Item Env:K5_STAGE03_SOURCE -ErrorAction SilentlyContinue
    Remove-Item Env:K5_STAGE03_CAM_CRED -ErrorAction SilentlyContinue
    Remove-Item Env:K5_STAGE_ONE_RECORDING_ROOT -ErrorAction SilentlyContinue

    Write-Host "Recording is disabled. Test media and temporary K5 state are not retained."
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
            name = $deviceName
            host = $sourceIp
            management_port = $rtspPort
            kind = "camera"
            protocols = @("rtsp")
            tags = $deviceTags
        }
    )
    if ([string]::IsNullOrWhiteSpace($device.id)) { throw "K5 alpha test device enrollment failed." }

    Write-Host "Launching the authenticated K5 Windows operator path..."
    $operatorHeaders = @{ Authorization = "Bearer $sessionToken" }
    $receipt = Invoke-RestMethod -Method Post -Uri "$baseUri/api/v1/operator/live" -Headers $operatorHeaders -ContentType "application/json" -TimeoutSec 60 -Body (
        ConvertTo-K5Json @{
            device_id = $device.id
            stream_token = $streamToken
            width = 1280
            height = 720
        }
    )
    if (-not $receipt.completed -or $receipt.delivered_frames -lt 1 -or $receipt.presentations -lt 1) {
        throw "K5 Windows operator alpha test did not complete cleanly."
    }

    Write-Host ("K5 operator PASS: frames={0}, presentations={1}" -f $receipt.delivered_frames, $receipt.presentations)
    Write-Host "No test-stream recording or retained media was created."
    if ($ExitAfterPublicTest) {
        Write-Host "Exiting after one bounded alpha acceptance run."
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
    if ($null -ne $publisherProcess -and -not $publisherProcess.HasExited) {
        Stop-Process -Id $publisherProcess.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $publisherProcess.Id -ErrorAction SilentlyContinue
    }
    if ($null -ne $mediaMtxProcess -and -not $mediaMtxProcess.HasExited) {
        Stop-Process -Id $mediaMtxProcess.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $mediaMtxProcess.Id -ErrorAction SilentlyContinue
    }
    foreach ($name in @(
        "K5_CONTROL_PLANE_SITE_ID","K5_DEVICE_DB_PATH","K5_USER_DB_PATH",
        "K5_CONTROL_PLANE_TOKEN","K5_CONTROL_PLANE_ADMIN_TOKEN",
        "K5_GSTREAMER_ROOT","GST_REGISTRY_1_0","K5_STAGE_ONE_RECORDING_ROOT",
        "K5_PUBLIC_TEST_RTSP_SOURCE","K5_PUBLIC_TEST_SOURCE_IP","K5_LOCAL_TEST_RTSP_SOURCE",
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
    $env:PATH = $priorPath
    if ($null -eq $priorGioProxyResolver) { Remove-Item Env:GIO_USE_PROXY_RESOLVER -ErrorAction SilentlyContinue }
    else { $env:GIO_USE_PROXY_RESOLVER = $priorGioProxyResolver }
    if ($null -eq $priorGioModuleDir) { Remove-Item Env:GIO_MODULE_DIR -ErrorAction SilentlyContinue }
    else { $env:GIO_MODULE_DIR = $priorGioModuleDir }
    if ($null -eq $priorNoProxy) { Remove-Item Env:no_proxy -ErrorAction SilentlyContinue }
    else { $env:no_proxy = $priorNoProxy }
    if ($null -eq $priorNoProxyUpper) { Remove-Item Env:NO_PROXY -ErrorAction SilentlyContinue }
    else { $env:NO_PROXY = $priorNoProxyUpper }
    $publicSourceIp = $null
}
