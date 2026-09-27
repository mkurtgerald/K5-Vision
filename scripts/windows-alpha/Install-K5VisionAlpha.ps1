[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"),
    [string]$GStreamerVersion = "1.28.7",
    [string]$K5Revision = "0fc10949a105357ff607a21866c5333f4d4be0c7"
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }
if ($GStreamerVersion -notmatch '^1\.28\.\d+$') { throw "Unreviewed GStreamer version." }
if ($K5Revision -notmatch '^[0-9a-fA-F]{40}$') { throw "K5Revision must be an exact SHA." }

$preflightSource = Join-Path $PSScriptRoot "Test-K5VisionAlpha.ps1"
$provisioner = Join-Path (Split-Path $PSScriptRoot -Parent) "provision-stage03-gstreamer.ps1"
foreach ($path in @($preflightSource, $provisioner)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required alpha bootstrap file is missing: $path" }
}
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($null -eq $py) { throw "Python 3.12 is required." }
& $py.Source -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required." }

$priorRunnerTemp = $env:RUNNER_TEMP
try {
    if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) { $env:RUNNER_TEMP = $env:TEMP }
    & $provisioner -Version $GStreamerVersion
    if ($LASTEXITCODE -ne 0) { throw "GStreamer provisioning failed." }
}
finally {
    if ([string]::IsNullOrWhiteSpace($priorRunnerTemp)) { Remove-Item Env:RUNNER_TEMP -ErrorAction SilentlyContinue }
    else { $env:RUNNER_TEMP = $priorRunnerTemp }
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$venv = Join-Path $InstallRoot ".venv"
& $py.Source -3.12 -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
$packageUri = "https://github.com/mkurtgerald/K5-Vision/archive/$K5Revision.zip"
& $python -m pip install $packageUri
if ($LASTEXITCODE -ne 0) { throw "K5 Vision Alpha installation failed." }
& $python -m k5vision.cli --version
if ($LASTEXITCODE -ne 0) { throw "Installed K5 CLI verification failed." }

$preflightTarget = Join-Path $InstallRoot "Test-K5VisionAlpha.ps1"
Copy-Item -LiteralPath $preflightSource -Destination $preflightTarget -Force
Set-Content -LiteralPath (Join-Path $InstallRoot "gstreamer-version.txt") -Value $GStreamerVersion -Encoding Ascii -NoNewline
Set-Content -LiteralPath (Join-Path $InstallRoot "k5-revision.txt") -Value $K5Revision.ToLowerInvariant() -Encoding Ascii -NoNewline
& $preflightTarget -InstallRoot $InstallRoot
if ($LASTEXITCODE -ne 0) { throw "K5 Alpha camera-free preflight failed." }
Write-Host "K5 Vision Alpha runtime installed from reviewed commit $K5Revision."
Write-Host "Camera-free preflight passed; no camera media was contacted or stored."
