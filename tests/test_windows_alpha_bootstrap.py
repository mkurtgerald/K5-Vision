from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALPHA = ROOT / "scripts" / "windows-alpha"
INSTALL = ALPHA / "Install-K5VisionAlpha.ps1"
PREFLIGHT = ALPHA / "Test-K5VisionAlpha.ps1"
RUN = ALPHA / "Run-K5VisionAlpha.ps1"
START = ALPHA / "Start-K5VisionAlpha.ps1"
PROVISION = ROOT / "scripts" / "provision-stage03-gstreamer.ps1"
PIN = "0cb2620ed59dae1bc997aa48810291b6f3b5f1b7"


def test_windows_alpha_bootstrap_files_exist() -> None:
    for path in (INSTALL, PREFLIGHT, RUN, START, PROVISION):
        assert path.is_file(), path


def test_windows_alpha_installer_is_revision_pinned_and_checkout_free() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert PIN in text
    assert "https://github.com/mkurtgerald/K5-Vision/archive/$K5Revision.zip" in text
    assert "K5Revision -notmatch '^[0-9a-fA-F]{40}$'" in text
    assert "pip install $repoRoot" not in text


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
    assert "rtspTransports: [tcp]" in text
    assert "videotestsrc" in text
    assert "rtspclientsink" in text
    assert "rtsp://127.0.0.1:8554/k5synthetic" in text
    assert "K5_LOCAL_TEST_RTSP_SOURCE" in text
    assert '"alpha-local-synthetic","ephemeral","non-recording"' in text
    assert "Local synthetic RTSP source PASS." in text
    assert '"x264enc","speed-preset=ultrafast","tune=zerolatency"' in text
    assert "format=I420" in text
    assert "foreach ($attempt in 1..20)" in text
    assert "$probe.WaitForExit(3000)" in text
    assert "Synthetic RTSP diagnostics:" in text
    assert "publisher.stderr.log" in text
    assert "mediamtx.stderr.log" in text


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
