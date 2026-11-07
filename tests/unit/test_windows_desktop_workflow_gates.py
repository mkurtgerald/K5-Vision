"""Keep independent Windows upgrade qualification separate from beta release admission."""

from pathlib import Path


_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "operator-desktop-windows.yml"
)


def test_failed_gui_analytics_does_not_skip_independent_upgrade_reversal():
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    native, upgrade = workflow.split("  cross-revision-upgrade:\n", 1)
    desktop = native.split("  native-desktop:\n", 1)[1].split("  vm-clock:\n", 1)[0]
    candidate = (
        "k5-vision-windows-upgrade-candidate-"
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    qualified = (
        "k5-vision-windows-setup-"
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )

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
