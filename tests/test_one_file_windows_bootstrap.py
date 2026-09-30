from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "Install-K5VisionAlpha.ps1"
PIN = "5d8b972cb7f9190294b759eb4fe55697b89933d4"


def test_one_file_windows_bootstrap_exists() -> None:
    assert BOOTSTRAP.is_file()


def test_one_file_windows_bootstrap_is_exact_revision_pinned() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert f'$K5Revision = "{PIN}"' in text
    assert "https://github.com/mkurtgerald/K5-Vision/archive/$K5Revision.zip" in text
    assert "Expand-Archive" in text
    assert "Install-K5VisionAlpha.ps1" in text
    assert "-K5Revision $K5Revision" in text


def test_one_file_windows_bootstrap_verifies_installed_revision_and_cleans_up() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'Join-Path $InstallRoot "k5-revision.txt"' in text
    assert "if ($actualRevision -ne $K5Revision)" in text
    assert "Remove-Item -LiteralPath $bootstrapRoot -Recurse -Force" in text
    assert "K5 Vision Alpha installation PASS." in text
    assert "non-recording local synthetic RTSP operator test" in text
