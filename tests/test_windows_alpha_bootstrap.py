from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALPHA = ROOT / "scripts" / "windows-alpha"
INSTALL = ALPHA / "Install-K5VisionAlpha.ps1"
PREFLIGHT = ALPHA / "Test-K5VisionAlpha.ps1"
PROVISION = ROOT / "scripts" / "provision-stage03-gstreamer.ps1"
PIN = "0fc10949a105357ff607a21866c5333f4d4be0c7"


def test_windows_alpha_bootstrap_files_exist() -> None:
    assert INSTALL.is_file()
    assert PREFLIGHT.is_file()
    assert PROVISION.is_file()


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
    assert "gstreamer-version.txt" in text


def test_windows_alpha_installer_runs_camera_free_preflight() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "Test-K5VisionAlpha.ps1" in text
    assert "& $preflightTarget -InstallRoot $InstallRoot" in text


def test_windows_alpha_preflight_verifies_runtime_without_camera_contact() -> None:
    text = PREFLIGHT.read_text(encoding="utf-8")
    assert "gst-launch-1.0.exe" in text
    assert "k5vision.cli --version" in text
    assert "No camera was contacted" in text
    assert "K5_STAGE03_SOURCE" not in text
    assert "/api/v1/operator/live" not in text


def test_windows_alpha_bootstrap_does_not_enable_recording() -> None:
    combined = INSTALL.read_text(encoding="utf-8") + PREFLIGHT.read_text(encoding="utf-8")
    assert "K5_STAGE_ONE_RECORDING_ROOT" not in combined
    assert "recording_root" not in combined
