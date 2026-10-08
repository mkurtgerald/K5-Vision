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
