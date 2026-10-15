from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_review_only_offline_wheelhouse_uses_strict_existing_verifier():
    script = (ROOT / "scripts/build_owner_offline_wheelhouse.py").read_text(encoding="utf-8")
    assert "tx.OfflineWheelhouse(" in script
    assert "runtime_source_mismatch" in script
    assert "len(paths) == 30" in script
    assert "manifest_mutation" in script
    assert "not_granted" in script
    assert "urllib.request" not in script
    assert "requests.get" not in script


def test_windows_ci_collects_exact_closed_wheels_without_owner_network():
    text = (ROOT / ".github/workflows/owner-windows-installer-candidate.yml").read_text(
        encoding="utf-8"
    )
    assert "pip download --no-deps --only-binary=:all:" in text
    assert "pip wheel --no-deps --wheel-dir $wheels $env:GITHUB_WORKSPACE" in text
    assert "K5_OFFLINE_WHEELHOUSE_EXTERNAL_APPROVAL=not_granted" in text
    assert "unqualified-owner-offline-manifest-" in text


def test_windows_git_checkout_preserves_exact_requirements_lf_bytes():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "scripts/windows-alpha/runtime-requirements.txt text eol=lf" in attributes
