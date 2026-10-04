[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Output)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$outputPath = [IO.Path]::GetFullPath($Output)
$pin = [regex]::Matches((Get-Content (Join-Path $repoRoot "Install-K5VisionAlpha.ps1") -Raw), '(?m)^\$K5Revision = "([0-9a-f]{40})"\r?$')
if ($pin.Count -ne 1) { throw "Alpha witness requires one exact reviewed revision." }
$revision = $pin[0].Groups[1].Value
$reviewedBaselineRevision = "d531d50d479f46af6ceed324a7cc379745becb61"
if ($revision -cne $reviewedBaselineRevision) { throw "Installed Alpha baseline revision requires review." }
# Independent immutable source identity, not the newer candidate launcher or a
# hash reported by installed code. Verified from this revision's Git tree:
# https://github.com/mkurtgerald/K5-Vision/blob/d531d50d479f46af6ceed324a7cc379745becb61/scripts/windows-alpha/Start-K5VisionAlpha.ps1
$expectedLauncherSize = 20863
$expectedLauncherBlob = "fe1ca98340a6967de0fa92a86dd361efa6dc6805"
$expectedLauncherHash = "e63fa030ec254bd4b8fa83cf90abd08fdfe351bf6fb4c40a215b1ad7bd60dd55"
function Get-K5CanonicalHash([string]$Path) {
    # Ignore checkout line-ending conversion, but compare all source content.
    $text = [IO.File]::ReadAllText($Path).Replace("`r`n", "`n")
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($text)))).Replace("-", "").ToLowerInvariant() }
    finally { $hasher.Dispose() }
}
$expectedHash = Get-K5CanonicalHash (Join-Path $repoRoot "src\k5vision\media\gstreamer_direct_frame_delivery.py")
$workRoot = Join-Path $env:RUNNER_TEMP ("k5-alpha-witness-" + [Guid]::NewGuid().ToString("N"))
$installRoot = Join-Path $workRoot "installed"
$hostExe = (Get-Process -Id $PID).Path
$stage = "download_payload"
$failureCode = "none"
$completed = $false
$runs = @()
$preserved = @{}
foreach ($name in @("RUNNER_TOOL_CACHE", "GITHUB_ENV", "GITHUB_PATH", "RUNNER_TEMP", "TEMP", "TMP", "PYTHONPATH")) {
    $preserved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
New-Item -ItemType Directory -Force -Path $workRoot | Out-Null

function Assert-K5PinnedLauncherBytes {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [ValidateRange(1,65536)][int]$ExpectedSize,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedBlob,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedSha256
    )
    $stream = $null
    $sha256 = $null
    $sha1 = $null
    try {
        $attributes = [IO.File]::GetAttributes($Path)
        if ($attributes -band ([IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint)) {
            throw "launcher_mismatch"
        }
        # Read only the declared bounded byte count under a non-write-sharing
        # handle. No newline conversion or decoded-text equivalence is accepted.
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        if ($stream.Length -ne $ExpectedSize) { throw "launcher_mismatch" }
        $bytes = New-Object byte[] $ExpectedSize
        $offset = 0
        while ($offset -lt $ExpectedSize) {
            $count = $stream.Read($bytes, $offset, $ExpectedSize - $offset)
            if ($count -eq 0) { throw "launcher_mismatch" }
            $offset += $count
        }
        if ($stream.ReadByte() -ne -1) { throw "launcher_mismatch" }
        $header = [Text.Encoding]::ASCII.GetBytes("blob " + $ExpectedSize + [char]0)
        $gitBytes = New-Object byte[] ($header.Length + $ExpectedSize)
        [Array]::Copy($header, 0, $gitBytes, 0, $header.Length)
        [Array]::Copy($bytes, 0, $gitBytes, $header.Length, $ExpectedSize)
        $sha256 = [Security.Cryptography.SHA256]::Create()
        $sha1 = [Security.Cryptography.SHA1]::Create()
        $actualSha256 = ([BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
        $actualBlob = ([BitConverter]::ToString($sha1.ComputeHash($gitBytes))).Replace("-", "").ToLowerInvariant()
        if ($actualSha256 -cne $ExpectedSha256 -or $actualBlob -cne $ExpectedBlob) {
            throw "launcher_mismatch"
        }
    } catch {
        throw "launcher_mismatch"
    } finally {
        if ($null -ne $sha1) { $sha1.Dispose() }
        if ($null -ne $sha256) { $sha256.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Invoke-K5Bounded([string]$Executable, [string[]]$Arguments, [int]$Seconds) {
    $stdout = Join-Path $workRoot "child.stdout.log"
    $stderr = Join-Path $workRoot "child.stderr.log"
    $child = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    # PowerShell 5.1 must cache the native handle before waiting, otherwise
    # redirected Start-Process can lose ExitCode for an already-exited child.
    $childHandle = $child.Handle
    try {
        if (-not $child.WaitForExit($Seconds * 1000)) {
            # Only terminate the child tree launched by this invocation, never a
            # process found by name, port, or an unrelated installed alpha path.
            if (-not $child.HasExited) {
                & taskkill.exe /PID $child.Id /T /F *> $null
                if (-not $child.WaitForExit(5000)) { throw "owned_child_cleanup_failed" }
            }
            throw "bounded_child_timeout"
        }
        $text = ""
        foreach ($path in @($stdout, $stderr)) {
            if ((Get-Item -LiteralPath $path).Length -gt 1048576) { throw "output_bound" }
            $text += [IO.File]::ReadAllText($path)
        }
        if ($child.ExitCode -ne 0) {
            $code = [regex]::Match($text, '\b(decoder_init_failure|decoder_failure|delivery_timeout|consumer_timeout|consumer_failure|windows_open_failure)\b')
            if ($code.Success) { throw $code.Value }
            throw "child_failed"
        }
        return $text
    } finally {
        $child.Dispose()
        Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    }
}

try {
    # Reproduce the user's LOCALAPPDATA runtime location, without changing the
    # job's existing cache configuration or exporting provisioning environment.
    foreach ($name in @("RUNNER_TOOL_CACHE", "GITHUB_ENV", "GITHUB_PATH", "PYTHONPATH")) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    $temporary = Join-Path $workRoot "temporary"
    New-Item -ItemType Directory -Force -Path $temporary | Out-Null
    foreach ($name in @("RUNNER_TEMP", "TEMP", "TMP")) {
        [Environment]::SetEnvironmentVariable($name, $temporary, "Process")
    }
    # Run the pinned archive's installer so launcher and Python code cannot
    # silently come from different commits under one installed revision label.
    $archive = Join-Path $workRoot "payload.zip"
    Invoke-WebRequest -Uri "https://github.com/mkurtgerald/K5-Vision/archive/$revision.zip" -OutFile $archive -UseBasicParsing -TimeoutSec 30
    $extracted = Join-Path $workRoot "source"
    Expand-Archive -LiteralPath $archive -DestinationPath $extracted
    $payloadRoot = Join-Path $extracted ("K5-Vision-" + $revision)
    $installer = Join-Path $payloadRoot "scripts\windows-alpha\Install-K5VisionAlpha.ps1"
    if (-not (Test-Path -LiteralPath $installer)) { throw "payload_unavailable" }
    $payloadLauncher = Join-Path $payloadRoot "scripts\windows-alpha\Start-K5VisionAlpha.ps1"
    Assert-K5PinnedLauncherBytes -Path $payloadLauncher -ExpectedSize $expectedLauncherSize -ExpectedBlob $expectedLauncherBlob -ExpectedSha256 $expectedLauncherHash
    $stage = "install"
    $null = Invoke-K5Bounded $hostExe @("-NoProfile", "-NonInteractive", "-File", ('"' + $installer + '"'), "-InstallRoot", ('"' + $installRoot + '"'), "-K5Revision", $revision, "-SkipDesktopShortcut") 300

    $stage = "installed_runtime"
    if ((Get-Content (Join-Path $installRoot "k5-revision.txt") -Raw).Trim() -ne $revision) { throw "revision_mismatch" }
    $python = Join-Path $installRoot ".venv\Scripts\python.exe"
    $hashCode = "import hashlib,pathlib; import k5vision.media.gstreamer_direct_frame_delivery as m; print(hashlib.sha256(pathlib.Path(m.__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest())"
    $actualHash = (Invoke-K5Bounded $python @("-c", ('"' + $hashCode + '"')) 15).Trim()
    if ($actualHash -ne $expectedHash) { throw "runtime_mismatch" }
    $installedLauncher = Join-Path $installRoot "Start-K5VisionAlpha.ps1"
    Assert-K5PinnedLauncherBytes -Path $installedLauncher -ExpectedSize $expectedLauncherSize -ExpectedBlob $expectedLauncherBlob -ExpectedSha256 $expectedLauncherHash
    $installedLauncherHash = Get-K5CanonicalHash $installedLauncher
    if ($installedLauncherHash -ne $expectedLauncherHash) { throw "launcher_mismatch" }

    $stage = "decoder_inventory"
    $gstBin = Join-Path $env:LOCALAPPDATA "K5RunnerTools\k5-gstreamer\1.28.7\msvc_x86_64\bin"
    $inspect = Join-Path $gstBin "gst-inspect-1.0.exe"
    $inventory = Invoke-K5Bounded $inspect @("d3d11h264dec") 15
    if ($inventory -notmatch '(?m)^\s*Version\s+1\.28\.7\s*$') { throw "decoder_version_mismatch" }
    $inventory = $null

    foreach ($attempt in 1..2) {
        $stage = "live_run_$attempt"
        # The shipped launcher refuses an occupied fixed RTSP port. Select only
        # the control-plane port here; never stop an existing listener to make room.
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        try { $listener.Start(); $port = $listener.LocalEndpoint.Port }
        finally { $listener.Stop() }
        $launcher = Join-Path $installRoot "Run-K5VisionAlpha.ps1"
        # No PublicRtspSource argument: the exact shipped generated local fixture
        # drives auth, enrollment, RTSP/TCP decode and the real Windows operator.
        $text = Invoke-K5Bounded $hostExe @("-NoProfile", "-NonInteractive", "-File", ('"' + $launcher + '"'), "-InstallRoot", ('"' + $installRoot + '"'), "-Port", $port, "-ExitAfterPublicTest") 90
        $receipts = [regex]::Matches($text, '(?m)^K5 operator PASS: frames=([0-9]{1,6}), presentations=([0-9]{1,6})\r?$')
        if ($receipts.Count -ne 1) { throw "completion_missing" }
        $frames = [int]$receipts[0].Groups[1].Value
        $presentations = [int]$receipts[0].Groups[2].Value
        if ($frames -ne 225 -or $presentations -lt 225) { throw "presentation_missing" }
        if ($text -notmatch 'Exiting after one bounded alpha acceptance run\.') { throw "bounded_exit_missing" }
        if (@(Get-ChildItem -LiteralPath $temporary -Filter "K5VisionAlpha-*" -Force).Count -ne 0) { throw "session_cleanup_incomplete" }
        $runs += @{ completed = $true; delivered_frames = $frames; presentations = $presentations }
        $text = $null
    }
    $stage = "complete"
    $completed = $true
} catch {
    $allowed = @("bounded_child_timeout", "owned_child_cleanup_failed", "output_bound", "child_failed", "revision_mismatch", "runtime_mismatch", "launcher_mismatch", "payload_unavailable", "decoder_version_mismatch", "completion_missing", "presentation_missing", "bounded_exit_missing", "session_cleanup_incomplete", "decoder_init_failure", "decoder_failure", "delivery_timeout", "consumer_timeout", "consumer_failure", "windows_open_failure")
    $failureCode = if ($allowed -contains $_.Exception.Message) { $_.Exception.Message } else { "unexpected" }
} finally {
    foreach ($name in $preserved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $preserved[$name], "Process")
    }
    Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
    $cleaned = -not (Test-Path -LiteralPath $workRoot)
    if (-not $cleaned) { $completed = $false; $failureCode = "cleanup_incomplete" }
    $evidence = @{
        schema_version = "1"; revision = $revision; normalized_runtime_sha256 = $expectedHash
        normalized_launcher_sha256 = $expectedLauncherHash
        scope = "installed_local_synthetic_direct_rtsp"; decoder = "d3d11h264dec"
        completed = $completed; stage = $stage; failure_code = $failureCode
        runs = $runs; cleanup_complete = $cleaned
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $outputPath -Parent) | Out-Null
    $evidence | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $outputPath -Encoding Ascii
}
if (-not $completed) { throw "K5 alpha direct RTSP witness failed: stage=$stage; code=$failureCode" }
Write-Host "K5 installed alpha direct RTSP PASS: two completed 225-frame Windows presentations; cleanup complete."
