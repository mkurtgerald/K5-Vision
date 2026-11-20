"""Guard early Windows installer startup against solid-archive preflight stalls."""

from pathlib import Path


def test_inno_pre_wizard_guard_has_independent_archive_blocks():
    """The native guard is extracted before setup UI; its lookup must be bounded."""
    script = (
        Path(__file__).resolve().parents[2] / "windows" / "desktop" / "K5Vision.iss"
    ).read_text(encoding="utf-8")
    assert "SolidCompression=no" in script
    assert "Compression=zip/7" in script
    assert "ExtractTemporaryFile('K5VisionInstallGuard.exe')" in script
    assert 'DestName: "K5VisionInstallGuard.exe"; Flags: dontcopy' in script
    assert "Result := AdmittedGuard(" in script
    assert "procedure AdmitExtractedPayload();" in script
    assert "'--installation-payload-check'" in script
