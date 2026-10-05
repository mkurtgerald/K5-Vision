[CmdletBinding()]
param([string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"))

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

# Keep this bounded pump identical to Start; source-hash parity is regression tested.
function Invoke-K5NativeProbe {
    param([string]$Executable, [string[]]$Arguments,
          [switch]$CaptureOutput, [switch]$DiscardStderr)
    $child = $null
    $primaryFailure = $null
    $started = $false
    $childHandle = [IntPtr]::Zero
    $outRead = $null; $errRead = $null
    $stdoutStream = $null; $stderrStream = $null
    $capturedStdout = $null
    try {
        if (-not [IO.Path]::IsPathRooted($Executable)) {
            throw [Management.Automation.CommandNotFoundException]::new(
                'K5 native application required.')
        }
        if ($null -eq $Arguments -or $Arguments.Count -lt 1 -or $Arguments.Count -gt 16) {
            throw [ArgumentException]::new('K5 native arguments invalid.')
        }
        # Encode one bounded Windows argv vector without a shell. Double every
        # backslash before a quote and every trailing backslash inside quotes.
        $quotedArguments = [Collections.Generic.List[string]]::new()
        foreach ($argument in $Arguments) {
            if ($null -eq $argument -or $argument.Length -gt 8192 -or
                $argument.IndexOf([char]0) -ge 0) {
                throw [ArgumentException]::new('K5 native arguments invalid.')
            }
            $encoded = [Text.StringBuilder]::new()
            [void]$encoded.Append([char]34)
            $slashes = 0
            foreach ($character in $argument.ToCharArray()) {
                if ($character -eq [char]92) { $slashes++; continue }
                if ($character -eq [char]34) {
                    [void]$encoded.Append([char]92, (2 * $slashes + 1))
                } else { [void]$encoded.Append([char]92, $slashes) }
                [void]$encoded.Append($character)
                $slashes = 0
            }
            [void]$encoded.Append([char]92, (2 * $slashes))
            [void]$encoded.Append([char]34)
            $quotedArguments.Add($encoded.ToString())
        }
        $command = Get-Command -Name $Executable -ErrorAction Stop
        if ($command -isnot [Management.Automation.ApplicationInfo] -or
            -not [string]::Equals([IO.Path]::GetFullPath($command.Path),
                [IO.Path]::GetFullPath($Executable), [StringComparison]::OrdinalIgnoreCase)) {
            throw [Management.Automation.CommandNotFoundException]::new(
                'K5 native application required.')
        }
        $info = [Diagnostics.ProcessStartInfo]::new()
        $info.FileName = $command.Path
        $info.Arguments = [string]::Join(' ', $quotedArguments.ToArray())
        if ($info.Arguments.Length -gt 16384) {
            throw [ArgumentException]::new('K5 native arguments invalid.')
        }
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardInput = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        if ($CaptureOutput) { $capturedStdout = [IO.MemoryStream]::new() }
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
                    if ($CaptureOutput) { $capturedStdout.Write($outBuffer, 0, $count) }
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
        if ($errCount -ne 0 -and -not $DiscardStderr) {
            throw [IO.InvalidDataException]::new('K5 native stderr refused.')
        }
        $outputText = ''
        if ($CaptureOutput) {
            $outputText = [Text.UTF8Encoding]::new($false, $true).GetString($capturedStdout.ToArray())
        }
        return [pscustomobject]@{ ExitCode = $exitCode; Stdout = $outputText }
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
        foreach ($ownedStream in @($stdoutStream, $stderrStream, $capturedStdout)) {
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

$python = Join-Path $InstallRoot ".venv\Scripts\python.exe"
$revisionRecord = Join-Path $InstallRoot "k5-revision.txt"
$versionRecord = Join-Path $InstallRoot "gstreamer-version.txt"
$launcher = Join-Path $InstallRoot "Start-K5VisionAlpha.ps1"
$runtime = Join-Path $InstallRoot "Run-K5VisionAlpha.ps1"
foreach ($path in @($python, $revisionRecord, $versionRecord, $launcher, $runtime)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "K5 Alpha preflight is missing a required installed file." }
}

$revision = (Get-Content -LiteralPath $revisionRecord -Raw).Trim()
if ($revision -notmatch '^[0-9a-f]{40}$') { throw "Installed K5 revision record is invalid." }
$gstreamerVersion = (Get-Content -LiteralPath $versionRecord -Raw).Trim()
if ($gstreamerVersion -notmatch '^1\.28\.\d+$') { throw "Installed GStreamer version record is invalid." }

$gstreamerRoot = Join-Path $env:LOCALAPPDATA "K5RunnerTools\k5-gstreamer\$gstreamerVersion\msvc_x86_64"
$gstLaunch = Join-Path $gstreamerRoot "bin\gst-launch-1.0.exe"
$gstCoreReady = (Test-Path -LiteralPath (Join-Path $gstreamerRoot "bin\gstreamer-1.0-0.dll")) -or
    (Test-Path -LiteralPath (Join-Path $gstreamerRoot "bin\libgstreamer-1.0-0.dll"))
if (-not $gstCoreReady -or -not (Test-Path -LiteralPath $gstLaunch)) { throw "Reviewed GStreamer runtime is incomplete." }

# Reuse the installed launcher's source-isolated, bounded config admission.
# Refusal happens before any native runtime probe or alpha session creation.
& $launcher -AnalyticsPreflightOnly

$gstVersion = Invoke-K5NativeProbe -Executable $gstLaunch -Arguments @("--version") -CaptureOutput -DiscardStderr
if ($gstVersion.ExitCode -ne 0 -or -not $gstVersion.Stdout.Contains("GStreamer $gstreamerVersion")) {
    throw "Reviewed GStreamer runtime version verification failed."
}
$cliVersion = Invoke-K5NativeProbe -Executable $python -Arguments @("-I", "-B", "-m", "k5vision.cli", "--version") -CaptureOutput
if ($cliVersion.ExitCode -ne 0) { throw "Installed K5 CLI verification failed." }
[Console]::Out.Write($cliVersion.Stdout)
$cliHelp = Invoke-K5NativeProbe -Executable $python -Arguments @("-I", "-B", "-m", "k5vision.cli", "--help") -DiscardStderr
if ($cliHelp.ExitCode -ne 0) { throw "Installed K5 CLI smoke test failed." }

Write-Host "K5 Vision Alpha preflight PASS"
Write-Host "Reviewed K5 revision: $revision"
Write-Host "Reviewed GStreamer: $gstreamerVersion"
Write-Host "No camera was contacted and no camera media was read or written by this preflight."
