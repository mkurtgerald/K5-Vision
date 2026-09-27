from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALPHA = ROOT / "scripts" / "windows-alpha"
INSTALL = ALPHA / "Install-K5VisionAlpha.ps1"
PREFLIGHT = ALPHA / "Test-K5VisionAlpha.ps1"
RUN = ALPHA / "Run-K5VisionAlpha.ps1"
START = ALPHA / "Start-K5VisionAlpha.ps1"
PROVISION = ROOT / "scripts" / "provision-stage03-gstreamer.ps1"
PIN = "2b2ef1a6d64bbc9df14271c95d2d0d14a19b7077"


def test_windows_alpha_bootstrap_files_exist() -> None:
    for path in (INSTALL, PREFLIGHT, RUN, START, PROVISION):
        assert path.is_file(), path


def test_windows_alpha_installer_is_revision_pinned_and_checkout_free() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert PIN in text
    assert "https://github.com/mkurtgerald/K5-Vision/archive/$K5Revision.zip" in text
    assert "K5Revision -notmatch '^[0-9a-fA-F]{40}$'" in text
    assert "pip install $repoRoot" not in text


def test_windows_alpha_installer_uses_reviewed_media_provisioner() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "provision-stage03-gstreamer.ps1" in text
    assert "& $provisioner -Version $GStreamerVersion" in text


def test_windows_alpha_installer_materializes_launcher_and_shortcut() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "Start-K5VisionAlpha.ps1" in text
    assert "Run-K5VisionAlpha.ps1" in text
    assert "K5 Vision Alpha.lnk" in text
    assert "-ExecutionPolicy Bypass -File" in text


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
    assert "K5_DEVICE_DB_PATH" in text and "$sessionRoot" in text
    assert "K5_USER_DB_PATH" in text and "$sessionRoot" in text
    assert "Remove-Item -LiteralPath $sessionRoot -Recurse -Force" in text


def test_windows_alpha_runtime_wrapper_uses_installed_launcher() -> None:
    text = RUN.read_text(encoding="utf-8")
    assert 'Join-Path $InstallRoot "Start-K5VisionAlpha.ps1"' in text
    assert "& $launcher -Port $Port" in text


def test_windows_alpha_bootstrap_does_not_enable_recording_or_camera_source() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (INSTALL, PREFLIGHT, RUN, START)
    )
    assert "K5_STAGE_ONE_RECORDING_ROOT" in combined
    assert "K5_STAGE03_SOURCE" not in combined
    assert "/api/v1/operator/live" not in combined
