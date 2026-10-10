[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"),
    [string]$GStreamerVersion = "1.28.7",
    [string]$K5Revision = "d531d50d479f46af6ceed324a7cc379745becb61",
    [string]$Wheelhouse = "",
    [string]$WheelhouseManifest = "",
    [string]$WheelhouseManifestSha256 = "",
    [string]$PythonExecutable = "",
    [switch]$SkipDesktopShortcut
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }
if ($GStreamerVersion -notmatch '^1\.28\.\d+$') { throw "Unreviewed GStreamer version." }
if ($K5Revision -notmatch '^[0-9a-fA-F]{40}$') { throw "K5Revision must be an exact SHA." }
$offlineArguments = @("Wheelhouse", "WheelhouseManifest", "WheelhouseManifestSha256")
$offlineCount = 0
foreach ($name in $offlineArguments) {
    if ($PSBoundParameters.ContainsKey($name)) { $offlineCount += 1 }
}
if ($offlineCount -ne 0 -and $offlineCount -ne 3) { throw "All offline wheelhouse arguments are required." }
if ($offlineCount -eq 3 -and
    ([string]::IsNullOrWhiteSpace($Wheelhouse) -or
     [string]::IsNullOrWhiteSpace($WheelhouseManifest) -or
     $WheelhouseManifestSha256 -cnotmatch '^[0-9a-f]{64}$')) {
    throw "Invalid offline wheelhouse arguments."
}

$preflightSource = Join-Path $PSScriptRoot "Test-K5VisionAlpha.ps1"
$launcherSource = Join-Path $PSScriptRoot "Start-K5VisionAlpha.ps1"
$runtimeSource = Join-Path $PSScriptRoot "Run-K5VisionAlpha.ps1"
$runtimeRequirements = Join-Path $PSScriptRoot "runtime-requirements.txt"
$provisioner = Join-Path (Split-Path $PSScriptRoot -Parent) "provision-stage03-gstreamer.ps1"
$transactionInstaller = Join-Path $PSScriptRoot "install_transaction.py"
foreach ($path in @($preflightSource, $launcherSource, $runtimeSource, $runtimeRequirements, $provisioner, $transactionInstaller)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required alpha bootstrap file is missing: $path" }
}

$pythonCommand = $null
$pythonPrefixArgs = @()
$pythonIsolation = @()
if ($offlineCount -eq 3) { $pythonIsolation = @("-I", "-S", "-B") }

# A bundled, private CPython is the only explicit interpreter override. This
# prevents a graphical installer from depending on PATH or touching a user's
# unrelated Python installation. The interpreter remains isolated under the
# exact requested K5 install root.
if (-not [string]::IsNullOrWhiteSpace($PythonExecutable)) {
    if (-not [IO.Path]::IsPathRooted($PythonExecutable)) {
        throw "Bundled Python must have an absolute path."
    }
    $expectedBundled = [IO.Path]::GetFullPath((Join-Path $InstallRoot "python312\python.exe"))
    $requestedBundled = [IO.Path]::GetFullPath($PythonExecutable)
    if ($requestedBundled -cne $expectedBundled -or -not [IO.File]::Exists($requestedBundled)) {
        throw "Only the K5-bundled Python executable is allowed."
    }
    & $requestedBundled -I -S -B -c "import struct, sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 1)"
    if ($LASTEXITCODE -ne 0) { throw "Bundled CPython 3.12 x64 verification failed." }
    $pythonCommand = $requestedBundled
}

if ($null -eq $pythonCommand) {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        & $py.Source -3.12 @pythonIsolation -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
        if ($LASTEXITCODE -eq 0) {
            $pythonCommand = $py.Source
            $pythonPrefixArgs = @("-3.12")
        }
    }
}

if ($null -eq $pythonCommand) {
    foreach ($candidateName in @("python3.12.exe", "python.exe")) {
        $candidate = Get-Command $candidateName -ErrorAction SilentlyContinue
        if ($null -eq $candidate) { continue }
        & $candidate.Source @pythonIsolation -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
        if ($LASTEXITCODE -eq 0) {
            $pythonCommand = $candidate.Source
            $pythonPrefixArgs = @()
            break
        }
    }
}

if ($null -eq $pythonCommand) { throw "Python 3.12 is required." }

# The transaction helper owns the install lock, staged verification, activation,
# and rollback. It never terminates running processes or moves an installed venv
# into service at a different path.
$hostCommand = Get-Command powershell.exe -ErrorAction SilentlyContinue
if ($null -eq $hostCommand) { $hostCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue }
if ($null -eq $hostCommand) { throw "A PowerShell host is required to verify the K5 installation." }
$desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($desktop) -and -not $SkipDesktopShortcut) {
    throw "Desktop path is unavailable."
}
$arguments = @(
    $transactionInstaller,
    "--install-root", $InstallRoot,
    "--source", $PSScriptRoot,
    "--powershell", $hostCommand.Source,
    "--revision", $K5Revision,
    "--gstreamer", $GStreamerVersion
)
# Supply the desktop path even when skipping a new shortcut: an interrupted
# earlier upgrade may still need to restore its original shortcut.
if (-not [string]::IsNullOrWhiteSpace($desktop)) {
    $arguments += @("--shortcut", (Join-Path $desktop "K5 Vision Alpha.lnk"))
}
if ($SkipDesktopShortcut) { $arguments += "--skip-shortcut" }
if ($offlineCount -eq 3) {
    $arguments += @("--wheelhouse", $Wheelhouse,
                   "--wheelhouse-manifest", $WheelhouseManifest,
                   "--wheelhouse-manifest-sha256", $WheelhouseManifestSha256)
}
$priorRunnerTemp = $env:RUNNER_TEMP
$priorRunnerToolCache = $env:RUNNER_TOOL_CACHE
try {
    if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) { $env:RUNNER_TEMP = $env:TEMP }
    # The GUI, first-launch preflight and operator launcher resolve GStreamer
    # at the user's LocalAppData path. A hosted runner's tool cache must not
    # redirect the owner-bundled runtime away from that contract.
    if (-not [string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $env:RUNNER_TOOL_CACHE = Join-Path $env:LOCALAPPDATA "K5RunnerTools"
    }
    & $pythonCommand @pythonPrefixArgs @pythonIsolation @arguments
    if ($LASTEXITCODE -ne 0) { throw "K5 Vision Alpha transaction failed; see recovery details above." }
}
finally {
    if ([string]::IsNullOrWhiteSpace($priorRunnerToolCache)) { Remove-Item Env:RUNNER_TOOL_CACHE -ErrorAction SilentlyContinue }
    else { $env:RUNNER_TOOL_CACHE = $priorRunnerToolCache }
    if ([string]::IsNullOrWhiteSpace($priorRunnerTemp)) { Remove-Item Env:RUNNER_TEMP -ErrorAction SilentlyContinue }
    else { $env:RUNNER_TEMP = $priorRunnerTemp }
}
