"""Keep independent Windows upgrade qualification separate from beta release admission."""

from pathlib import Path

_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "operator-desktop-windows.yml"
)


def test_hash_pinned_gui_compiler_retries_only_transient_transport_faults():
    """A GitHub release outage may be retried, never a mismatched compiler."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    downloader = workflow.split("Provision exact reviewed Inno Setup compiler", 1)[1].split(
        "Build click-through native Windows installer", 1
    )[0]
    assert "for ($attempt = 1; $attempt -le 3; $attempt++)" in downloader
    assert "@(408, 429, 500, 502, 503, 504)" in downloader
    assert "Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue" in downloader
    assert 'throw "Reviewed Inno Setup archive transport unavailable"' in downloader
    assert downloader.index("Invoke-WebRequest -Uri $identity.url") < downloader.index(
        "Get-FileHash -LiteralPath $archive -Algorithm SHA256"
    )
    assert "(Get-FileHash -LiteralPath $archive -Algorithm SHA256)" in downloader
    assert "$actual.Length -ne $identity.bytes" in downloader
    assert (
        "Native Inno Setup compiler 6.4.3 admitted by exact official archive SHA256" in downloader
    )


def test_failed_gui_analytics_does_not_skip_independent_upgrade_reversal():
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    native, upgrade = workflow.split("  cross-revision-upgrade:\n", 1)
    desktop = native.split("  native-desktop:\n", 1)[1].split("  vm-clock:\n", 1)[0]
    candidate = (
        "k5-vision-windows-upgrade-candidate-"
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    qualified = "k5-vision-windows-setup-${{ github.event.pull_request.head.sha || github.sha }}"

    assert (
        desktop.index("Build click-through native Windows installer")
        < desktop.index("Upload non-release candidate")
        < desktop.index("Exercise actual installer, first-run account")
        < desktop.index("Upload qualified graphical Windows installer")
    )
    assert desktop.count(candidate) == 1
    assert desktop.count(qualified) == 1
    assert "qualification_status='unqualified'" in desktop
    assert "desktop-build/upgrade-candidate/" in desktop
    assert "desktop-build/release/" in desktop

    assert "needs: [native-desktop, focused-windows]" in upgrade
    assert "if: ${{ always()" in upgrade
    assert "needs.focused-windows.result == 'success'" in upgrade
    assert "needs.native-desktop.result == 'failure'" in upgrade
    assert 'qualification_status -ne "unqualified"' in upgrade
    assert upgrade.count(candidate) == 1
    assert qualified not in upgrade
    # The independent upgrade test still executes real graphical install,
    # live-refusal, upgrade and manual reversal; it cannot assert beta release.
    assert "Exercise real graphical baseline, live-refusal, upgrade and reversal" in upgrade
