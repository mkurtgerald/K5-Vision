[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"),
    [string]$GStreamerVersion = "1.28.7",
    [string]$K5Revision = "0ffeaffc157921df92f776dc08b8e2e9dbc982bd",
    [switch]$SkipDesktopShortcut
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }
if ($GStreamerVersion -notmatch '^1\.28\.\d+$') { throw "Unreviewed GStreamer version." }
if ($K5Revision -notmatch '^[0-9a-fA-F]{40}$') { throw "K5Revision must be an exact SHA." }

$preflightSource = Join-Path $PSScriptRoot "Test-K5VisionAlpha.ps1"
$launcherSource = Join-Path $PSScriptRoot "Start-K5VisionAlpha.ps1"
$runtimeSource = Join-Path $PSScriptRoot "Run-K5VisionAlpha.ps1"
$provisioner = Join-Path (Split-Path $PSScriptRoot -Parent) "provision-stage03-gstreamer.ps1"
foreach ($path in @($preflightSource, $launcherSource, $runtimeSource, $provisioner)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required alpha bootstrap file is missing: $path" }
}

$pythonCommand = $null
$pythonPrefixArgs = @()

$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($null -ne $py) {
    & $py.Source -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
    if ($LASTEXITCODE -eq 0) {
        $pythonCommand = $py.Source
        $pythonPrefixArgs = @("-3.12")
    }
}

if ($null -eq $pythonCommand) {
    foreach ($candidateName in @("python3.12.exe", "python.exe")) {
        $candidate = Get-Command $candidateName -ErrorAction SilentlyContinue
        if ($null -eq $candidate) { continue }
        & $candidate.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
        if ($LASTEXITCODE -eq 0) {
            $pythonCommand = $candidate.Source
            $pythonPrefixArgs = @()
            break
        }
    }
}

if ($null -eq $pythonCommand) { throw "Python 3.12 is required." }

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
& $pythonCommand @pythonPrefixArgs -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
$packageUri = "https://github.com/mkurtgerald/K5-Vision/archive/$K5Revision.zip"
& $python -m pip install --force-reinstall --no-deps $packageUri
if ($LASTEXITCODE -ne 0) { throw "K5 Vision Alpha installation failed." }
& $python -m k5vision.cli --version
if ($LASTEXITCODE -ne 0) { throw "Installed K5 CLI verification failed." }
$runtimeProbe = "from k5vision.operator_runtime import LOCAL_TEST_SOURCE_ENV; raise SystemExit(0 if LOCAL_TEST_SOURCE_ENV == 'K5_LOCAL_TEST_RTSP_SOURCE' else 1)"
& $python -c $runtimeProbe
if ($LASTEXITCODE -ne 0) { throw "Installed K5 runtime does not match the reviewed alpha capabilities." }
Write-Host "Installed K5 Python runtime capability verification PASS."

$preflightTarget = Join-Path $InstallRoot "Test-K5VisionAlpha.ps1"
$launcherTarget = Join-Path $InstallRoot "Start-K5VisionAlpha.ps1"
$runtimeTarget = Join-Path $InstallRoot "Run-K5VisionAlpha.ps1"
Copy-Item -LiteralPath $preflightSource -Destination $preflightTarget -Force
Copy-Item -LiteralPath $launcherSource -Destination $launcherTarget -Force
Copy-Item -LiteralPath $runtimeSource -Destination $runtimeTarget -Force
Set-Content -LiteralPath (Join-Path $InstallRoot "gstreamer-version.txt") -Value $GStreamerVersion -Encoding Ascii -NoNewline
Set-Content -LiteralPath (Join-Path $InstallRoot "k5-revision.txt") -Value $K5Revision.ToLowerInvariant() -Encoding Ascii -NoNewline

& $preflightTarget -InstallRoot $InstallRoot
if ($LASTEXITCODE -ne 0) { throw "K5 Alpha camera-free preflight failed." }

if (-not $SkipDesktopShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if ([string]::IsNullOrWhiteSpace($desktop)) { throw "Desktop path is unavailable." }
    $shortcutPath = Join-Path $desktop "K5 Vision Alpha.lnk"
    $hostCommand = Get-Command powershell.exe -ErrorAction SilentlyContinue
    if ($null -eq $hostCommand) { $hostCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue }
    if ($null -eq $hostCommand) { throw "A PowerShell host is required to create the K5 launcher." }

    $wsh = New-Object -ComObject WScript.Shell
    $shortcut = $wsh.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $hostCommand.Source
    $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -NoExit -File "' + $launcherTarget + '"'
    $shortcut.WorkingDirectory = $InstallRoot
    $shortcut.Description = "K5 Vision Windows Alpha"
    $shortcut.Save()
    if (-not (Test-Path -LiteralPath $shortcutPath)) {
        throw "K5 Vision Alpha desktop shortcut creation failed."
    }
    Write-Host "Desktop shortcut created: $shortcutPath"
}

Write-Host "K5 Vision Alpha runtime installed from reviewed commit $K5Revision."
Write-Host "Camera-free preflight passed; no camera media was contacted or stored."
