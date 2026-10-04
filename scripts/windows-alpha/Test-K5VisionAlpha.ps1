[CmdletBinding()]
param([string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"))

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

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

$output = @(& $gstLaunch --version 2>$null)
$gstLaunchSucceeded = $?
if (-not $gstLaunchSucceeded -or -not (($output -join "`n").Contains("GStreamer $gstreamerVersion"))) {
    throw "Reviewed GStreamer runtime version verification failed."
}
& $python -I -B -m k5vision.cli --version
$cliVersionSucceeded = $?
if (-not $cliVersionSucceeded) { throw "Installed K5 CLI verification failed." }
& $python -I -B -m k5vision.cli --help *> $null
$cliHelpSucceeded = $?
if (-not $cliHelpSucceeded) { throw "Installed K5 CLI smoke test failed." }

Write-Host "K5 Vision Alpha preflight PASS"
Write-Host "Reviewed K5 revision: $revision"
Write-Host "Reviewed GStreamer: $gstreamerVersion"
Write-Host "No camera was contacted and no camera media was read or written by this preflight."
