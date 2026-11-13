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
    desktop = native.split("  native-desktop:\n", 1)[1].split("  camera-video:\n", 1)[0]
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

    assert "needs: [native-desktop]" in upgrade
    assert "if: ${{ always() && !cancelled() &&" in upgrade
    assert "focused-windows" not in upgrade
    assert "needs.native-desktop.result == 'failure'" in upgrade
    assert 'qualification_status -ne "unqualified"' in upgrade
    assert upgrade.count(candidate) == 1
    assert qualified not in upgrade
    # The independent upgrade test still executes real graphical install,
    # live-refusal, upgrade and manual reversal; it cannot assert beta release.
    assert "Exercise real graphical baseline, live-refusal, upgrade and reversal" in upgrade


def test_historical_baseline_transport_retry_never_weakens_upgrade_admission():
    """Retry historical official-archive HTTP refusals, not pin or product failures."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    baseline = workflow.split("Build complete native baseline package from pinned source", 1)[
        1
    ].split("Compile exact historical graphical supervisor", 1)[0]
    assert "ref: b21cdfb1bb5b8605c77f7dac0957cc6651bd0bd5" in workflow
    assert "for ($attempt = 1; $attempt -le 3; $attempt++)" in baseline
    assert "baseline-source/scripts/build_desktop_payload.py" in baseline
    assert "--head-sha $env:K5_DESKTOP_BASELINE_HEAD_SHA" in baseline
    assert '"stage": "native_workers", "error_type": "HTTPError"' in baseline
    assert "if (-not $retryable -or $attempt -eq 3)" in baseline
    assert 'throw "Previously qualified baseline payload could not be reproduced"' in baseline
    assert "Remove-Item -LiteralPath baseline-build -Recurse -Force" in baseline
    assert "Remove-Item -LiteralPath $log -Force" in baseline
    assert "Get-Content -LiteralPath $log -Raw" in baseline
    assert "Write-Host $output" not in baseline
    assert "source_url" not in baseline
    assert "K5_DONOR_TOOL_ROOT" not in baseline


def test_two_clock_vms_run_independently_with_the_same_qualified_producer_inputs():
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    clock = workflow.split("  vm-clock:\n", 1)[1].split("  cross-revision-upgrade:\n", 1)[0]
    assert (
        "name: Exercise real clock corrections on disposable Windows VM (${{ matrix.clock_run }}/2)"
        in clock
    )
    assert "needs: [native-desktop]" in clock
    assert "focused-windows" not in clock
    assert "runs-on: windows-2025" in clock
    assert "strategy:\n      fail-fast: false\n      matrix:\n        clock_run: [1, 2]" in clock
    assert "max-parallel:" not in clock
    assert "continue-on-error:" not in clock
    assert (
        "K5_VM_CLOCK_SETUP_ARTIFACT_ID: ${{ needs.native-desktop.outputs.setup_artifact_id }}"
        in clock
    )
    assert (
        "K5_VM_CLOCK_PRIMARY_ARTIFACT_ID: "
        "${{ needs.native-desktop.outputs.primary_witness_artifact_id }}" in clock
    )
    assert "K5_VM_CLOCK_PRIMARY_RUN_ID: ${{ github.run_id }}" in clock
    assert (
        "K5_VM_CLOCK_PRIMARY_RUN_ATTEMPT: ${{ needs.native-desktop.outputs.primary_run_attempt }}"
        in clock
    )
    assert "artifact-ids: ${{ needs.native-desktop.outputs.setup_artifact_id }}" in clock
    assert "artifact-ids: ${{ needs.native-desktop.outputs.primary_witness_artifact_id }}" in clock
    assert "--expected-primary-artifact-id $env:K5_VM_CLOCK_PRIMARY_ARTIFACT_ID" in clock
    assert "--expected-setup-artifact-id $env:K5_VM_CLOCK_SETUP_ARTIFACT_ID" in clock


def test_clock_matrix_retains_original_witness_name_and_distinct_repeat_artifact():
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    clock = workflow.split("  vm-clock:\n", 1)[1].split("  cross-revision-upgrade:\n", 1)[0]
    evidence = clock.split("Preserve finite VM clock evidence after UTC restoration", 1)[1]
    assert "if: always()" in evidence
    assert (
        "name: ${{ matrix.clock_run == 1 && "
        "format('vm-clock-windows-witness-{0}', github.run_id) || "
        "format('vm-clock-repeat-{0}-windows-witness-{1}', matrix.clock_run, github.run_id) }}"
        in evidence
    )
    assert "vm-clock-windows.xml" in evidence
    assert "vm-clock-windows-receipt.json" in evidence
    assert "vm-clock-input/desktop-package.json" in evidence
