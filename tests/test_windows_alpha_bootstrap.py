import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALPHA = ROOT / "scripts" / "windows-alpha"
INSTALL = ALPHA / "Install-K5VisionAlpha.ps1"
PREFLIGHT = ALPHA / "Test-K5VisionAlpha.ps1"
RUN = ALPHA / "Run-K5VisionAlpha.ps1"
START = ALPHA / "Start-K5VisionAlpha.ps1"
PROVISION = ROOT / "scripts" / "provision-stage03-gstreamer.ps1"
PIN = "d531d50d479f46af6ceed324a7cc379745becb61"


def test_windows_alpha_bootstrap_files_exist() -> None:
    for path in (INSTALL, PREFLIGHT, RUN, START, PROVISION):
        assert path.is_file(), path


def test_windows_alpha_installer_is_revision_pinned_and_checkout_free() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert PIN in text
    assert "https://github.com/mkurtgerald/K5-Vision/archive/$K5Revision.zip" in text
    assert "K5Revision -notmatch '^[0-9a-fA-F]{40}$'" in text
    assert "pip install $repoRoot" not in text
    assert "-m pip install --force-reinstall --no-deps $packageUri" in text
    assert "LOCAL_TEST_SOURCE_ENV" in text
    assert "Installed K5 Python runtime capability verification PASS." in text


def test_windows_alpha_installer_accepts_python_312_without_legacy_launcher() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "Get-Command py.exe -ErrorAction SilentlyContinue" in text
    assert '"python3.12.exe", "python.exe"' in text
    assert "$pythonCommand = $candidate.Source" in text
    assert "& $pythonCommand @pythonPrefixArgs -m venv $venv" in text


def test_windows_alpha_installer_uses_reviewed_media_provisioner() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "provision-stage03-gstreamer.ps1" in text
    assert "& $provisioner -Version $GStreamerVersion" in text


def test_windows_alpha_installer_materializes_launcher_and_shortcut() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "Start-K5VisionAlpha.ps1" in text
    assert "Run-K5VisionAlpha.ps1" in text
    assert "K5 Vision Alpha.lnk" in text
    assert "-ExecutionPolicy Bypass -NoExit -File" in text
    assert "[switch]$SkipDesktopShortcut" in text
    assert "if (-not $SkipDesktopShortcut)" in text


def test_windows_alpha_preflight_verifies_installed_runtime_without_camera_contact() -> None:
    text = PREFLIGHT.read_text(encoding="utf-8")
    assert "Start-K5VisionAlpha.ps1" in text
    assert "Run-K5VisionAlpha.ps1" in text
    assert "gst-launch-1.0.exe" in text
    assert "gstreamer-1.0-0.dll" in text
    assert "libgstreamer-1.0-0.dll" in text
    assert "k5vision.cli --version" in text
    assert "No camera was contacted" in text
    assert "K5_STAGE03_SOURCE" not in text


def test_windows_alpha_launcher_is_loopback_ephemeral_and_non_recording() -> None:
    text = START.read_text(encoding="utf-8")
    assert '"--host","127.0.0.1"' in text
    assert "/api/v1/health" in text
    assert "K5_STAGE_ONE_RECORDING_ROOT" in text
    assert "K5_GSTREAMER_ROOT" in text
    assert "GST_REGISTRY_1_0" in text
    assert 'Join-Path $gstreamerRoot "bin"' in text
    assert "$env:PATH = if ([string]::IsNullOrWhiteSpace($priorPath))" in text
    assert "$env:PATH = $priorPath" in text
    assert '$env:GIO_USE_PROXY_RESOLVER = "dummy"' in text
    assert "$env:GIO_MODULE_DIR = $gioModuleDir" in text
    assert '$env:no_proxy = "*"' in text
    assert '$env:NO_PROXY = "*"' in text
    assert "Remove-Item Env:GIO_USE_PROXY_RESOLVER" in text
    assert "Remove-Item Env:GIO_MODULE_DIR" in text
    assert "K5_DEVICE_DB_PATH" in text and "$sessionRoot" in text
    assert "K5_USER_DB_PATH" in text and "$sessionRoot" in text
    assert "Remove-Item -LiteralPath $sessionRoot -Recurse -Force" in text


def test_windows_alpha_runtime_wrapper_uses_installed_launcher() -> None:
    text = RUN.read_text(encoding="utf-8")
    assert 'Join-Path $InstallRoot "Start-K5VisionAlpha.ps1"' in text
    assert "& $launcher -Port $Port" in text
    assert "-PublicRtspSource $PublicRtspSource" in text
    assert "[switch]$ExitAfterPublicTest" in text
    assert "-ExitAfterPublicTest:$ExitAfterPublicTest" in text


def test_windows_alpha_defaults_to_verified_local_synthetic_rtsp() -> None:
    text = START.read_text(encoding="utf-8")
    assert '[string]$PublicRtspSource = ""' in text
    assert '$MediaMtxVersion = "1.21.1"' in text
    assert "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23" in text
    assert "mediamtx_v$MediaMtxVersion" in text
    assert "Get-FileHash" in text
    assert "rtspAddress: 127.0.0.1:8554" in text
    assert "rtspTransports: [tcp, udp]" in text
    assert "rtpAddress: 127.0.0.1:18000" in text
    assert "rtcpAddress: 127.0.0.1:18001" in text
    assert "videotestsrc" in text
    assert '"pattern=ball"' in text
    assert '"animation-mode=wall-time"' in text
    assert '"flip=true"' in text
    assert "rtspclientsink" in text
    assert "rtsp://127.0.0.1:8554/k5synthetic" in text
    assert "K5_LOCAL_TEST_RTSP_SOURCE" in text
    assert '$payloadType = "96"' in text
    assert "$env:K5_OPERATOR_RTP_PAYLOAD_TYPE = $payloadType" in text
    assert '"alpha-local-synthetic","ephemeral","non-recording"' in text
    assert "Local synthetic RTSP publisher PASS; K5 native media probe pending." in text
    assert '"x264enc","speed-preset=ultrafast","tune=zerolatency"' in text
    assert "format=I420" in text
    assert '"identity","eos-after=1"' not in text
    assert "probe.WaitForExit" not in text
    assert "deferring media readback to the K5 native live-source probe." in text
    assert "Synthetic RTSP diagnostics:" in text
    assert "publisher.stderr.log" in text
    assert (
        "Synthetic visual acceptance run complete. Swagger will not be opened automatically."
        in text
    )
    assert 'Test-K5TcpListener "127.0.0.1" 8554' in text
    assert "Local synthetic RTSP port 8554 is already in use." in text
    assert "source: publisher" not in text
    assert "--version" in text
    assert "--validate-conf" in text
    assert "Pinned MediaMTX executable failed its version probe." in text
    assert "Local synthetic RTSP MediaMTX configuration is invalid." in text
    assert 'Write-Host "Starting local MediaMTX RTSP server..."' in text
    assert "Start-Process -FilePath $mediaMtx" in text
    assert "-PassThru -NoNewWindow" in text
    assert (
        "-RedirectStandardOutput"
        not in text.split("$server = Start-Process -FilePath $mediaMtx", 1)[1].split(
            "$serverReady", 1
        )[0]
    )
    assert (
        "-WindowStyle Hidden"
        not in text.split("$server = Start-Process -FilePath $mediaMtx", 1)[1].split(
            "$serverReady", 1
        )[0]
    )


def test_windows_alpha_launches_authenticated_operator_without_recording() -> None:
    text = START.read_text(encoding="utf-8")
    assert '"--operator"' in text
    assert "[switch]$ExitAfterPublicTest" in text
    assert "Exiting after one bounded alpha acceptance run." in text
    assert "resolve_public_test_source_ip" in text
    assert "K5_PUBLIC_TEST_RTSP_SOURCE" in text
    assert "K5_PUBLIC_TEST_SOURCE_IP" in text
    assert "$env:K5_OPERATOR_STREAM_TOKEN = $streamToken" in text
    assert "Remove-Item Env:K5_STAGE_ONE_RECORDING_ROOT" in text
    assert "/api/v1/users" in text
    assert "/api/v1/auth/bootstrap-password" in text
    assert "/api/v1/auth/login" in text
    assert "/api/v1/devices" in text
    assert "/api/v1/operator/live" in text


def test_windows_alpha_test_does_not_retain_media_or_private_camera_config() -> None:
    text = START.read_text(encoding="utf-8")
    assert "No test-stream recording or retained media was created." in text
    assert "Set-Content" not in text
    assert "Remove-Item Env:K5_STAGE03_SOURCE" in text
    assert "Remove-Item Env:K5_STAGE03_CAM_CRED" in text
    assert "$env:K5_STAGE03_SOURCE =" not in text
    assert "$env:K5_STAGE03_CAM_CRED =" not in text


def test_windows_alpha_installer_cleans_stale_venv_process_before_rebuild() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "Get-CimInstance Win32_Process" in text
    assert "ExecutablePath" in text
    assert "Stopping stale K5 Vision Alpha runtime process" in text
    assert "Stop-Process -Id ([int]$staleProcess.ProcessId) -Force" in text
    assert "Remove-Item -LiteralPath $venv -Recurse -Force" in text
    assert "Existing K5 Vision Alpha runtime is still locked." in text


def test_windows_alpha_launcher_rejects_occupied_control_plane_port() -> None:
    text = START.read_text(encoding="utf-8")
    guard = 'Test-K5TcpListener "127.0.0.1" $Port'
    assert guard in text
    assert "K5 Vision Alpha control-plane port $Port is already in use." in text
    assert text.index(guard) < text.index('$arguments = @("-m","k5vision.cli","serve"')
    assert '$health.status -eq "ok" -and -not $process.HasExited' in text


def test_windows_alpha_installer_installs_reviewed_runtime_dependencies() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    requirements = ALPHA / "runtime-requirements.txt"
    assert requirements.is_file()
    dependency_text = requirements.read_text(encoding="utf-8")
    for requirement in (
        "fastapi==0.142.2",
        "onvif-python==0.3.1",
        "psutil==7.2.2",
        "pydantic==2.13.5",
        "uvicorn==0.54.0",
    ):
        assert requirement in dependency_text
    assert '$runtimeRequirements = Join-Path $PSScriptRoot "runtime-requirements.txt"' in text
    assert "-m pip install --requirement $runtimeRequirements" in text
    assert "-m pip install --force-reinstall --no-deps $packageUri" in text
    assert "-m pip check" in text
    assert "K5 Vision Alpha runtime dependency verification PASS." in text


def test_synthetic_source_owns_partial_startup_until_successful_return() -> None:
    text = START.read_text(encoding="utf-8")
    source = text.split("function Start-K5SyntheticSource {", 1)[1].split("\n$writeToken", 1)[0]
    assert "$server = $null\n    $publisher = $null\n    try {" in source
    assert "$serverHandle = $server.Handle" in source
    assert "$publisherHandle = $publisher.Handle" in source
    assert "foreach ($owned in @($publisher, $server))" in source
    assert "if (-not $owned.HasExited) { $owned.Kill() }" in source
    assert "$owned.WaitForExit(5000)" in source
    assert "throw $startupFailure" in source
    assert "owned process cleanup was incomplete" in source
    assert "Get-Process" not in source
    assert "Get-CimInstance" not in source


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows process cleanup")
def test_publisher_launch_failure_stops_owned_server_only(tmp_path: Path) -> None:
    launcher = str(START).replace("'", "''")
    temporary = str(tmp_path).replace("'", "''")
    script = r"""
$ErrorActionPreference = "Stop"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '__LAUNCHER__', [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Launcher parse failed.' }
$function = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Start-K5SyntheticSource'
}, $true)
if ($null -eq $function) { throw 'Synthetic source function missing.' }
. ([scriptblock]::Create($function.Extent.Text))
$hostExe = (Get-Process -Id $PID).Path
$sessionRoot = '__TEMPORARY__'
$MediaMtxVersion = '1.21.1'
$gstLaunch = 'injected-publisher'
$script:ownedServer = $null
$script:publisherAttempted = $false
function Test-K5GStreamerElement { return $true }
function Get-K5MediaMtx { return 'Test-K5MediaMtxExecutable' }
function Test-K5MediaMtxExecutable { $global:LASTEXITCODE = 0; return 'v1.21.1' }
function Test-K5TcpListener { return $null -ne $script:ownedServer }
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$PassThru, [switch]$NoNewWindow,
          $WindowStyle, $RedirectStandardOutput, $RedirectStandardError)
    if ($FilePath -eq 'injected-publisher') {
        $script:publisherAttempted = $true
        throw 'injected_publisher_launch_failure'
    }
    $arguments = @('-NoProfile', '-NonInteractive', '-Command', '"Start-Sleep -Seconds 30"')
    $script:ownedServer = Microsoft.PowerShell.Management\Start-Process `
        -FilePath $hostExe -ArgumentList $arguments -PassThru
    return $script:ownedServer
}
$foreign = Microsoft.PowerShell.Management\Start-Process -FilePath $hostExe `
    -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', '"Start-Sleep -Seconds 30"') `
    -PassThru
$foreignHandle = $foreign.Handle
try {
    try {
        $null = Start-K5SyntheticSource
        throw 'publisher_failure_was_accepted'
    } catch {
        if ($_.Exception.Message -ne 'injected_publisher_launch_failure') { throw }
    }
    if (-not $script:publisherAttempted) { throw 'Publisher was not attempted.' }
    if ($null -eq $script:ownedServer -or -not $script:ownedServer.HasExited) {
        throw 'Owned server survived publisher launch failure.'
    }
    if ($foreign.HasExited) { throw 'An unrelated process was terminated.' }
} finally {
    foreach ($child in @($script:ownedServer, $foreign)) {
        if ($null -ne $child -and -not $child.HasExited) {
            $child.Kill()
            $null = $child.WaitForExit(5000)
        }
    }
}
""".replace("__LAUNCHER__", launcher).replace("__TEMPORARY__", temporary)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
