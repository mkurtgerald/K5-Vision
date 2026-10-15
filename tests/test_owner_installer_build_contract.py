from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "scripts/windows-alpha/owner-installer/K5VisionAlpha.iss"
WORKFLOW = ROOT / ".github/workflows/owner-windows-installer-candidate.yml"
INSTALL = ROOT / "scripts/windows-alpha/Install-K5VisionAlpha.ps1"


def test_setup_has_native_exe_shortcuts_and_uninstall_without_owner_shell():
    text = ISS.read_text(encoding="utf-8")
    assert "OutputBaseFilename=K5VisionAlpha-Setup-{#K5Revision}" in text
    assert "PrivilegesRequired=lowest" in text
    assert "ArchitecturesAllowed=x64compatible" in text
    assert 'Name: "{autoprograms}\\K5 Vision Alpha"' in text
    assert 'Name: "{userdesktop}\\K5 Vision Alpha"' in text
    assert "UninstallDisplayIcon={app}\\K5VisionAlpha.exe" in text
    assert 'Source: "{#SourceRoot}\\build\\owner-installer\\python-3.12.10-amd64.exe"' in text
    assert "RunOrFail(" in text
    assert "-PythonExecutable " in text
    assert "-SkipDesktopShortcut" in text
    assert "RaiseException(" in text
    assert "CloseApplications=no" in text
    assert 'Type: filesandordirs; Name: "{app}\\.venv"' in text
    assert 'Type: filesandordirs; Name: "{app}"' not in text


def test_build_pins_source_and_python_vendor_binary_no_release_promotion():
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "ref: " + "$" + "{{ github.sha }}" in content
    assert "67B5635E80EA51072B87941312D00EC8927C4DB9BA18938F7AD2D27B328B95FB" in content
    assert "Get-AuthenticodeSignature" in content
    assert "unqualified-owner-installer-" in content
    assert "K5_OWNER_SETUP_INSTALL_LAUNCH_SHORTCUT_CHECK=passed" in content
    assert "actions/upload-artifact@" in content
    assert "actions/checkout@" in content
    assert "actions/create-release" not in content
    assert "[string]$PythonExecutable" in INSTALL.read_text(encoding="utf-8")


def test_owner_setup_requires_bundled_analytics_and_native_preflight():
    script = ISS.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for marker in (
        "owner_analytics_bundle.py",
        "analytics\\manifest.json",
        "analytics\\wheels\\*.whl",
        "analytics\\models\\*",
        "Analytics-Lab-LICENSE",
        "Analytics-Lab-THIRD_PARTY.md",
        "analytics-config.json",
        "analytics-models",
        "Verified owner analytics payload is missing.",
    ):
        assert marker in script
    for marker in (
        "ref: c8b347ae538991a0c0ce38eabc2dc17b566531d3",
        "scripts/build_owner_analytics_bundle.py",
        "K5_OWNER_ANALYTICS_BUNDLE=verified_engineering_only",
        "K5_OWNER_SETUP_INSTALLED_ANALYTICS_PREFLIGHT=passed",
        "analytics-preflight",
    ):
        assert marker in workflow
    transaction = (ROOT / "scripts/windows-alpha/install_transaction.py").read_text(
        encoding="utf-8"
    )
    for marker in (
        "analytics-config.json",
        "analytics-models",
        "materialize_models",
        "module.validate(self.analytics_bundle)",
        "Installed wheel closure mismatch",
    ):
        assert marker in transaction


def test_media_server_is_bundled_by_setup_and_never_downloaded_on_owner_host():
    script = (ROOT / "scripts/windows-alpha/Start-K5VisionAlpha.ps1").read_text(encoding="utf-8")
    installer = ISS.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    gui = (ROOT / "scripts/windows-alpha/owner-installer/K5VisionAlpha.cs").read_text(
        encoding="utf-8"
    )
    license_path = ROOT / "docs/licenses/MediaMTX-MIT.txt"
    expected = "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23"
    assert expected in script and expected in workflow
    assert "mediamtx_v1.21.1_windows_amd64.zip" in installer
    assert "MediaMTX-MIT.txt" in installer
    assert "MediaMTX-MIT.txt" in (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert license_path.is_file()
    assert "MIT License" in license_path.read_text(encoding="utf-8")
    assert r"payload\mediamtx\mediamtx_v1.21.1_windows_amd64.zip" in gui
    assert "return 11;" in gui
    assert "K5_OFFLINE_MEDIAMTX_BUNDLE=v1.21.1_sha256_verified" in workflow
    assert "Get-K5MediaMtx" in script and "Expand-Archive -LiteralPath $archive" in script
    assert "$sessionRoot" in script
    media_section = script.split("function Get-K5MediaMtx {", 1)[1].split(
        "function Start-K5SyntheticSource {", 1
    )[0]
    assert "Invoke-WebRequest" not in media_section
    assert "MediaMtxArchiveUri" not in script
    assert "https://github.com/bluenviron/mediamtx" not in script
    assert "Remove-Item -LiteralPath $root -Recurse -Force" not in media_section
    assert "MediaMtxArchiveSha256" in media_section


def test_gstreamer_owner_runtime_uses_hash_pinned_bundle_and_refuses_foreign_root():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    installer = ISS.read_text(encoding="utf-8")
    provision = (ROOT / "scripts/provision-stage03-gstreamer.ps1").read_text(encoding="utf-8")
    transaction = (ROOT / "scripts/windows-alpha/install_transaction.py").read_text(
        encoding="utf-8"
    )
    gui = (ROOT / "scripts/windows-alpha/owner-installer/K5VisionAlpha.cs").read_text(
        encoding="utf-8"
    )
    identity = "032fc6062b8539838fc8da22589cb9b24c5d820baa7f8cc160af9ea08395badf"
    assert identity in workflow and identity in provision
    assert "gstreamer-1.0-msvc-x86_64-1.28.7.exe" in installer
    assert "K5_OFFLINE_GSTREAMER_BUNDLE=1.28.7_sha256_verified" in workflow
    assert "Required pinned-build transfer executable unavailable." in workflow
    assert "GStreamer build-time transfer was incomplete." in workflow
    assert "--retry-max-time 420 --output $target $uri" in workflow
    assert "-OfflineInstaller" in transaction
    assert "if self.analytics_bundle is not None:" in transaction
    assert "if self.offline is None and not any(" in transaction
    assert "Bundled offline GStreamer runtime failed exact identity admission" in provision
    assert "no foreign changes permitted" in provision
    assert r"payload\gstreamer\gstreamer-1.0-msvc-x86_64-1.28.7.exe" in gui
    assert "return 12;" in gui
