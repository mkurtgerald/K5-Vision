[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"),
    [string]$GStreamerVersion = "1.28.7"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") { throw "Windows is required." }
if ($GStreamerVersion -notmatch '^1\.28\.\d+$') {
    throw "The GStreamer version is outside the reviewed K5 stable series."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$launcherSource = Join-Path $PSScriptRoot "Start-K5VisionAlpha.ps1"
$provisioner = Join-Path $PSScriptRoot "provision-stage03-gstreamer.ps1"
if (-not (Test-Path -LiteralPath $launcherSource)) {
    throw "Start-K5VisionAlpha.ps1 is missing."
}
if (-not (Test-Path -LiteralPath $provisioner)) {
    throw "The reviewed GStreamer provisioner is missing."
}

$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($null -eq $py) { throw "Python 3.12 is required." }
& $py.Source -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required." }

Write-Host "Provisioning the reviewed K5 media runtime..."
$priorRunnerTemp = $env:RUNNER_TEMP
try {
    if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
        $env:RUNNER_TEMP = $env:TEMP
    }
    & $provisioner -Version $GStreamerVersion
    if ($LASTEXITCODE -ne 0) { throw "GStreamer runtime provisioning failed." }
}
finally {
    if ([string]::IsNullOrWhiteSpace($priorRunnerTemp)) {
        Remove-Item Env:RUNNER_TEMP -ErrorAction SilentlyContinue
    } else {
        $env:RUNNER_TEMP = $priorRunnerTemp
    }
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$venv = Join-Path $InstallRoot ".venv"
& $py.Source -3.12 -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }

$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $python -m pip install $repoRoot
if ($LASTEXITCODE -ne 0) { throw "K5 Vision Alpha installation failed." }

$launcherTarget = Join-Path $InstallRoot "Start-K5VisionAlpha.ps1"
Copy-Item -LiteralPath $launcherSource -Destination $launcherTarget -Force
Set-Content -LiteralPath (Join-Path $InstallRoot "gstreamer-version.txt") -Value $GStreamerVersion -Encoding Ascii -NoNewline

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "K5 Vision Alpha.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcherTarget`""
$shortcut.WorkingDirectory = $InstallRoot
$shortcut.Description = "Launch K5 Vision Alpha"
$shortcut.Save()

Write-Host "K5 Vision Alpha installed."
Write-Host "Desktop shortcut: $shortcutPath"
Write-Host "This installer does not write camera media or camera credentials."
