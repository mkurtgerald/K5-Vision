from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gui_passes_closed_offline_manifest_and_wheels_from_separate_staging():
    inno = (ROOT / "scripts/windows-alpha/owner-installer/K5VisionAlpha.iss").read_text(
        encoding="utf-8"
    )
    for marker in (
        "#ifndef OfflineWheelhouseDigest",
        "Closed offline owner wheelhouse payload is missing.",
        "' -Wheelhouse ' + Q(ReviewedWheels)",
        "' -WheelhouseManifest ' + Q(ReviewedManifest)",
        "' -WheelhouseManifestSha256 {#OfflineWheelhouseDigest}'",
        'DestDir: "{tmp}\\k5-closed\\wheelhouse"',
        'DestDir: "{tmp}\\k5-closed\\scripts\\windows-alpha"',
        "' -Version 1.28.7 -OfflineInstaller '",
        "not DirExists(AppRoot + '\\.venv')",
    ):
        assert marker in inno
    assert "Q(Bootstrap) + ' -InstallRoot ' + Q(AppRoot)" in inno


def test_owner_build_embeds_manifest_hash_separately_and_never_promotes():
    workflow = (ROOT / ".github/workflows/owner-windows-installer-candidate.yml").read_text(
        encoding="utf-8"
    )
    assert '"/DOfflineWheelhouseDigest=$manifestSha"' in workflow
    assert "unqualified-owner-installer-" in workflow
    assert "K5_OFFLINE_WHEELHOUSE_EXTERNAL_APPROVAL=not_granted" in workflow
