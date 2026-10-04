[CmdletBinding()]
param(
    [ValidateRange(1024,65535)][int]$Port = 8000,
    [string]$PublicRtspSource = "",
    [switch]$ExitAfterPublicTest,
    [switch]$AnalyticsPreflightOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

$MediaMtxVersion = "1.21.1"
$MediaMtxArchiveSha256 = "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23"
$MediaMtxArchiveUri = "https://github.com/bluenviron/mediamtx/releases/download/v$MediaMtxVersion/mediamtx_v$MediaMtxVersion" + "_windows_amd64.zip"

# Keep admission in the installed launcher so Test and Start cannot drift. This
# child never receives a source URI or credential and cannot import the checkout.
# No temporary directory, output log, session database, or media process is made.
function Invoke-K5AnalyticsPreflight {
    param(
        [Parameter(Mandatory=$true)][string]$Python,
        [ValidateRange(1,60)][int]$TimeoutSeconds = 30
    )
    $maximumOutputBytes = 4096
    $failureMessage = "K5 analytics preflight failed. No alpha session was started."
    $child = $null
    $stdout = $null
    $started = $false
    try {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = $Python
        $info.Arguments = "-I -B -m k5vision.cli analytics-preflight"
        $info.WorkingDirectory = Split-Path -Parent $Python
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        # Inherit only Windows runtime/location variables and the explicit
        # selection. Never pass unrelated source, credential, or database state.
        # The CLI owns all package/model/config admission; the parent is unchanged.
        $allowedEnvironment = @(
            "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TEMP", "TMP",
            "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "K5_ANALYTICS_CONFIG"
        )
        foreach ($name in @($info.EnvironmentVariables.Keys)) {
            if ($name -notin $allowedEnvironment) { $info.EnvironmentVariables.Remove($name) }
        }
        $child = New-Object System.Diagnostics.Process
        $child.StartInfo = $info
        $started = $child.Start()
        if (-not $started) { throw $failureMessage }
        $childHandle = $child.Handle
        $stdout = New-Object System.IO.MemoryStream
        $outBuffer = New-Object byte[] 1024
        $errBuffer = New-Object byte[] 1
        $outRead = $child.StandardOutput.BaseStream.ReadAsync($outBuffer, 0, $outBuffer.Length)
        $errRead = $child.StandardError.BaseStream.ReadAsync($errBuffer, 0, $errBuffer.Length)
        $outDone = $false
        $errDone = $false
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            if ($watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) { throw $failureMessage }
            if (-not $outDone -and $outRead.IsCompleted) {
                $count = $outRead.GetAwaiter().GetResult()
                if ($count -eq 0) { $outDone = $true }
                else {
                    if ($stdout.Length + $count -gt $maximumOutputBytes) { throw $failureMessage }
                    $stdout.Write($outBuffer, 0, $count)
                    $outRead = $child.StandardOutput.BaseStream.ReadAsync($outBuffer, 0, $outBuffer.Length)
                }
            }
            if (-not $errDone -and $errRead.IsCompleted) {
                # Successful admission has no stderr. Reject immediately rather
                # than buffering an exception, path, credential, or output flood.
                if ($errRead.GetAwaiter().GetResult() -ne 0) { throw $failureMessage }
                $errDone = $true
            }
            if ($child.HasExited -and $outDone -and $errDone) { break }
            Start-Sleep -Milliseconds 10
        }
        if ($child.ExitCode -ne 0) { throw $failureMessage }
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $json = $utf8.GetString($stdout.ToArray()).Trim()
        # Match the CLI's canonical success records, never permissive JSON
        # coercion (arrays, duplicate/escaped keys, and extra fields must fail).
        if ($json -ceq '{"schema_version":"1","analytics_enabled":true,"status":"ready"}') {
            return $true
        }
        if ($json -ceq '{"schema_version":"1","analytics_enabled":false,"status":"disabled"}') {
            return $false
        }
        throw $failureMessage
    } catch {
        # Do not expose child output or raw Process/JSON/config exceptions.
        throw $failureMessage
    } finally {
        try {
            if ($started -and -not $child.HasExited) {
                $child.Kill()
                if (-not $child.WaitForExit(5000)) { throw $failureMessage }
            }
        } catch {
            throw $failureMessage
        } finally {
            if ($null -ne $child) { $child.Dispose() }
            if ($null -ne $stdout) { $stdout.Dispose() }
        }
    }
}

function Test-K5AlphaOperatorReceipt([object]$Receipt, [bool]$AnalyticsRequired) {
    if ($null -eq $Receipt) { return $false }
    $fields = @($Receipt.PSObject.Properties.Name)
    foreach ($name in @("completed", "delivered_frames", "presentations")) {
        if ($name -cnotin $fields) { return $false }
    }
    if ($Receipt.completed -isnot [bool] -or -not $Receipt.completed) { return $false }
    $counterNames = @("delivered_frames", "presentations")
    if ($AnalyticsRequired) {
        foreach ($name in @("analytics_enabled", "analytics_provider_submissions",
                            "analytics_provider_completions", "analytics_failures")) {
            if ($name -cnotin $fields) { return $false }
        }
        if ($Receipt.analytics_enabled -isnot [bool] -or -not $Receipt.analytics_enabled) {
            return $false
        }
        $counterNames += @("analytics_provider_submissions", "analytics_provider_completions",
                           "analytics_failures")
    }
    foreach ($name in $counterNames) {
        $counter = $Receipt.$name
        if (($counter -isnot [int] -and $counter -isnot [long]) -or $counter -lt 0) {
            return $false
        }
        if ($name -cne "analytics_failures" -and $counter -lt 1) { return $false }
    }
    if ($AnalyticsRequired -and ($Receipt.analytics_failures -ne 0 -or
        $Receipt.analytics_provider_completions -gt $Receipt.analytics_provider_submissions)) {
        return $false
    }
    return $true
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$versionRecord = Join-Path $PSScriptRoot "gstreamer-version.txt"
if (-not (Test-Path -LiteralPath $python)) { throw "Run Install-K5VisionAlpha.ps1 first." }
$analyticsRequired = Invoke-K5AnalyticsPreflight -Python $python
if ($analyticsRequired) { Write-Host "K5 analytics configuration admitted; live provider acceptance is pending." }
else { Write-Host "K5 analytics disabled; video-only alpha acceptance selected." }
if ($AnalyticsPreflightOnly) { return }
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
    $child = $null
    $primaryFailure = $null
    $started = $false
    $childHandle = [IntPtr]::Zero
    $outRead = $null; $errRead = $null
    $stdoutStream = $null; $stderrStream = $null
    try {
        # Only one bounded token reaches the native argv parser. This includes
        # actual element names and the admitted one-argument native probe.
        if ($Name.Length -eq 0 -or $Name.Length -gt 8192 -or
            $Name -match '[^\x21-\x7e]|["\\]' -or -not [IO.Path]::IsPathRooted($gstInspect)) {
            throw [Management.Automation.CommandNotFoundException]::new(
                'K5 native application required.')
        }
        $command = Get-Command -Name $gstInspect -ErrorAction Stop
        if ($command -isnot [Management.Automation.ApplicationInfo] -or
            -not [string]::Equals([IO.Path]::GetFullPath($command.Path),
                [IO.Path]::GetFullPath($gstInspect), [StringComparison]::OrdinalIgnoreCase)) {
            throw [Management.Automation.CommandNotFoundException]::new(
                'K5 native application required.')
        }
        $info = [Diagnostics.ProcessStartInfo]::new()
        $info.FileName = $command.Path
        $info.Arguments = $Name
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardInput = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $child = [Diagnostics.Process]::new()
        $child.StartInfo = $info
        $started = $child.Start()
        if (-not $started) { throw [InvalidOperationException]::new('K5 native start failed.') }
        $childHandle = $child.Handle
        $child.StandardInput.Close()
        $stdoutStream = $child.StandardOutput.BaseStream
        $stderrStream = $child.StandardError.BaseStream
        $outBuffer = [byte[]]::new(4096)
        $errBuffer = [byte[]]::new(4096)
        $outRead = $stdoutStream.ReadAsync($outBuffer, 0, $outBuffer.Length)
        $errRead = $stderrStream.ReadAsync($errBuffer, 0, $errBuffer.Length)
        $outDone = $false; $errDone = $false
        $outCount = 0; $errCount = 0
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            if ($watch.Elapsed.TotalSeconds -ge 5) {
                throw [TimeoutException]::new('K5 native timeout.')
            }
            if (-not $outDone -and $outRead.IsCompleted) {
                $count = $outRead.GetAwaiter().GetResult()
                if ($count -eq 0) { $outDone = $true }
                else {
                    $outCount += $count
                    if ($outCount + $errCount -gt 131072) {
                        throw [IO.InvalidDataException]::new('K5 native output limit.')
                    }
                    $outRead = $stdoutStream.ReadAsync($outBuffer, 0, $outBuffer.Length)
                }
            }
            if (-not $errDone -and $errRead.IsCompleted) {
                $count = $errRead.GetAwaiter().GetResult()
                if ($count -eq 0) { $errDone = $true }
                else {
                    $errCount += $count
                    if ($outCount + $errCount -gt 131072) {
                        throw [IO.InvalidDataException]::new('K5 native output limit.')
                    }
                    $errRead = $stderrStream.ReadAsync($errBuffer, 0, $errBuffer.Length)
                }
            }
            if ($child.HasExited -and $outDone -and $errDone) { break }
            [Threading.Thread]::Sleep(10)
        }
        $exitCode = $child.ExitCode
        if ($errCount -ne 0) { throw [IO.InvalidDataException]::new('K5 native stderr refused.') }
        return $exitCode -eq 0
    } catch {
        $primaryFailure = $_
        throw
    } finally {
        $cleanupWatch = [Diagnostics.Stopwatch]::StartNew()
        $cleanupFailed = $false
        try {
            if ($started) {
                if ($childHandle -eq [IntPtr]::Zero -or $child.Handle -ne $childHandle) {
                    throw [InvalidOperationException]::new('K5 native cleanup failed.')
                }
                if (-not $child.HasExited) {
                    $child.Kill()
                    if (-not $child.WaitForExit(5000)) {
                        throw [InvalidOperationException]::new('K5 native cleanup failed.')
                    }
                }
            }
        } catch { $cleanupFailed = $true }
        foreach ($ownedStream in @($stdoutStream, $stderrStream)) {
            try { if ($null -ne $ownedStream) { $ownedStream.Close() } }
            catch { $cleanupFailed = $true }
        }
        try { if ($null -ne $child) { $child.Dispose() } }
        catch { $cleanupFailed = $true }
        while (($null -ne $outRead -and -not $outRead.IsCompleted) -or
               ($null -ne $errRead -and -not $errRead.IsCompleted)) {
            if ($cleanupWatch.Elapsed.TotalSeconds -ge 5) {
                $cleanupFailed = $true
                break
            }
            [Threading.Thread]::Sleep(10)
        }
        if ($cleanupFailed) {
            $cleanupError = [InvalidOperationException]::new('K5 native cleanup failed.')
            if ($primaryFailure -is [Management.Automation.ErrorRecord]) {
                $cleanupError.Data['K5ElementPrimaryErrorRecord'] = $primaryFailure
            }
            throw $cleanupError
        }
        # The exact started Process and its streams are now closed.
    }
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
rtspTransports: [tcp, udp]
rtspAddress: 127.0.0.1:8554
rtpAddress: 127.0.0.1:18000
rtcpAddress: 127.0.0.1:18001
rtmp: false
hls: false
webrtc: false
srt: false
moq: false
paths:
  k5synthetic:
"@
    [IO.File]::WriteAllText($configPath, $config)

    $mediaMtxVersionOutput = @(& $mediaMtx --version 2>&1)
    if ($LASTEXITCODE -ne 0 -or $mediaMtxVersionOutput.Count -ne 1 -or
        $mediaMtxVersionOutput[0] -isnot [string] -or
        $mediaMtxVersionOutput[0] -cne ("v" + $MediaMtxVersion)) {
        foreach ($line in $mediaMtxVersionOutput) { Write-Host ("mediamtx-version: " + $line) }
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
    $server = $null
    $publisher = $null
    try {
        $server = Start-Process -FilePath $mediaMtx -ArgumentList @($configPath) -PassThru -NoNewWindow
        $serverHandle = $server.Handle
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
            throw "Local synthetic RTSP server failed to start."
        }

        $source = "rtsp://127.0.0.1:8554/k5synthetic"
        $publisherArgs = @(
            "-q",
            "videotestsrc","is-live=true","pattern=ball","animation-mode=wall-time","flip=true",
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
        $publisherHandle = $publisher.Handle

        Write-Host "Synthetic RTSP publisher started; deferring media readback to the K5 native live-source probe."
        Start-Sleep -Milliseconds 750
        if ($publisher.HasExited -or $server.HasExited) {
            $publisherState = if ($publisher.HasExited) { "exited:$($publisher.ExitCode)" } else { "running" }
            $serverState = if ($server.HasExited) { "exited:$($server.ExitCode)" } else { "running" }
            Write-Host ("Synthetic RTSP diagnostics: server={0}, publisher={1}" -f $serverState, $publisherState)
            if (Test-Path -LiteralPath $publisherErr) {
                $publisherTail = @(Get-Content -LiteralPath $publisherErr -Tail 8 -ErrorAction SilentlyContinue)
                foreach ($line in $publisherTail) { Write-Host ("publisher: " + $line) }
            }
            throw "Local synthetic RTSP source failed to remain available for K5 probing."
        }

        Write-Host "Local synthetic RTSP publisher PASS; K5 native media probe pending."
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
    } catch {
        # Ownership transfers only after a successful return. If publisher
        # creation fails, the outer launcher has not received either process.
        $startupFailure = $_
        $cleanupComplete = $true
        foreach ($owned in @($publisher, $server)) {
            if ($null -eq $owned) { continue }
            try {
                if (-not $owned.HasExited) { $owned.Kill() }
                if (-not $owned.WaitForExit(5000)) { $cleanupComplete = $false }
            } catch {
                $cleanupComplete = $false
            }
        }
        if (-not $cleanupComplete) {
            $cleanupFailure = [InvalidOperationException]::new(
                "Local synthetic RTSP startup failed and owned process cleanup was incomplete."
            )
            # Keep the original typed error only in memory for bounded diagnostic
            # projection. Cleanup remains fatal; never serialize this private link.
            $cleanupFailure.Data["K5.StartupErrorRecord"] = $startupFailure
            throw $cleanupFailure
        }
        throw $startupFailure
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

    if (Test-K5TcpListener "127.0.0.1" $Port) {
        throw "K5 Vision Alpha control-plane port $Port is already in use. Close any previous K5 Vision Alpha window or reinstall to clean the stale runtime."
    }

    $sourceUri = $null
    $sourceIp = $null
    $rtspPort = $null
    $streamToken = $null
    $deviceName = $null
    $deviceTags = $null
    $payloadType = $null

    if ([string]::IsNullOrWhiteSpace($PublicRtspSource)) {
        $synthetic = Start-K5SyntheticSource
        $sourceUri = $synthetic.Uri
        $sourceIp = $synthetic.Ip
        $rtspPort = $synthetic.Port
        $streamToken = $synthetic.StreamToken
        $deviceName = $synthetic.DeviceName
        $deviceTags = $synthetic.Tags
        $payloadType = "96"
        $mediaMtxProcess = $synthetic.ServerProcess
        $publisherProcess = $synthetic.PublisherProcess
        $env:K5_LOCAL_TEST_RTSP_SOURCE = $sourceUri
        Remove-Item Env:K5_PUBLIC_TEST_RTSP_SOURCE -ErrorAction SilentlyContinue
        Remove-Item Env:K5_PUBLIC_TEST_SOURCE_IP -ErrorAction SilentlyContinue
        Write-Host "Starting K5 Vision Alpha local synthetic operator test on http://127.0.0.1:$Port"
    } else {
        $resolveCode = "from k5vision.operator_runtime import resolve_public_test_source_ip; import sys; print(resolve_public_test_source_ip(sys.argv[1]))"
        $resolved = @(& $python -I -B -c $resolveCode $PublicRtspSource 2>$null)
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
    if ([string]::IsNullOrWhiteSpace([string]$payloadType)) {
        Remove-Item Env:K5_OPERATOR_RTP_PAYLOAD_TYPE -ErrorAction SilentlyContinue
    } else {
        $env:K5_OPERATOR_RTP_PAYLOAD_TYPE = $payloadType
    }
    Remove-Item Env:K5_STAGE03_SOURCE -ErrorAction SilentlyContinue
    Remove-Item Env:K5_STAGE03_CAM_CRED -ErrorAction SilentlyContinue
    Remove-Item Env:K5_STAGE_ONE_RECORDING_ROOT -ErrorAction SilentlyContinue

    Write-Host "Recording is disabled. Test media and temporary K5 state are not retained."
    $arguments = @("-m","k5vision.cli","serve","--operator","--host","127.0.0.1","--port",$Port)
    $arguments = @("-I","-B") + $arguments
    $process = Start-Process -FilePath $python -ArgumentList $arguments -PassThru -NoNewWindow -WorkingDirectory (Split-Path -Parent $python)

    $baseUri = "http://127.0.0.1:$Port"
    $ready = $false
    foreach ($attempt in 1..40) {
        if ($process.HasExited) { break }
        try {
            $health = Invoke-RestMethod -Method Get -Uri "$baseUri/api/v1/health" -TimeoutSec 1
            if ($health.status -eq "ok" -and -not $process.HasExited) { $ready = $true; break }
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
    if (-not (Test-K5AlphaOperatorReceipt -Receipt $receipt -AnalyticsRequired $analyticsRequired)) {
        throw "K5 Windows operator alpha test did not complete the selected acceptance checks."
    }
    if ($analyticsRequired) {
        Write-Host ("K5 analytics PASS: submissions={0}, completions={1}, failures=0" -f $receipt.analytics_provider_submissions, $receipt.analytics_provider_completions)
    }

    Write-Host ("K5 operator PASS: frames={0}, presentations={1}" -f $receipt.delivered_frames, $receipt.presentations)
    Write-Host "No test-stream recording or retained media was created."
    if ($ExitAfterPublicTest) {
        Write-Host "Exiting after one bounded alpha acceptance run."
        return
    }
    if ([string]::IsNullOrWhiteSpace($PublicRtspSource)) {
        Write-Host "Synthetic visual acceptance run complete. Swagger will not be opened automatically."
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
