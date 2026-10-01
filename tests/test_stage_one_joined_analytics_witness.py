"""Hosted regressions for the opt-in, source-free joined Windows witness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_WORKFLOW = Path(".github/workflows/stage-one-operator-physical.yml")
_TEST = "tests/integration/test_stage_one_joined_analytics_physical.py"
_REVISION = "a" * 40
_ANALYTICS_REVISION = "c8b347ae538991a0c0ce38eabc2dc17b566531d3"
_COUNTERS = {
    "delivered_frames": 6,
    "presentations": 6,
    "analytics_provider_submissions": 2,
    "analytics_provider_completions": 2,
    "analytics_rendered_boxes": 3,
}


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("joined_witness", _TEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metrics(**changes: object) -> SimpleNamespace:
    values = {**_COUNTERS, "analytics_enabled": True, "analytics_failures": 0, **changes}
    return SimpleNamespace(**values)


def _evidence(metrics: SimpleNamespace, **changes: object) -> dict[str, object]:
    parameters = {
        "provider_calls": 2,
        "tracked_detections": 1,
        "revision": _REVISION,
        "analytics_revision": _ANALYTICS_REVISION,
        **changes,
    }
    return _module()._source_free_evidence(metrics, **parameters)


def test_joined_receipt_retains_only_exact_revisions_and_accepted_aggregates() -> None:
    evidence = _evidence(_metrics(source_uri="rtsp://private.example/live", payload=b"private"))

    assert evidence == {
        "schema_version": "1",
        "revision": _REVISION,
        "analytics_revision": _ANALYTICS_REVISION,
        "execution_context": "reviewed-video-windows-x64",
        "windows_live_launch_completed": True,
        "analytics_enabled": True,
        "analytics_failures": 0,
        "analytics_provider_calls": 2,
        "analytics_tracked_detections": 1,
        **_COUNTERS,
    }


@pytest.mark.parametrize("field", _COUNTERS)
@pytest.mark.parametrize("value", [0, -1, True, "private", 1_000_001])
def test_joined_receipt_rejects_absent_or_invalid_positive_metrics(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match="positive bounded counters"):
        _evidence(_metrics(**{field: value}))


@pytest.mark.parametrize("field", ["provider_calls", "tracked_detections"])
@pytest.mark.parametrize("value", [0, -1, True, "private", 1_000_001])
def test_joined_receipt_rejects_absent_or_invalid_provider_metrics(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match="positive bounded counters"):
        _evidence(_metrics(), **{field: value})


@pytest.mark.parametrize(
    "changes",
    [{"analytics_enabled": False}, {"analytics_failures": 1}, {"analytics_failures": False}],
)
def test_joined_receipt_rejects_disabled_or_failed_analytics(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="successful analytics"):
        _evidence(_metrics(**changes))


@pytest.mark.parametrize("field", ["revision", "analytics_revision"])
@pytest.mark.parametrize("value", ["", "main", "a" * 39, "z" * 40, "rtsp://private.example/live"])
def test_joined_receipt_rejects_unbound_or_unsafe_revision(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="^joined witness requires exact reviewed revisions$"):
        _evidence(_metrics(), **{field: value})


@pytest.mark.parametrize("value, skipped", [("", True), ("0", True), ("true", True), ("1", False)])
def test_joined_physical_witness_requires_its_specific_opt_in(
    monkeypatch: pytest.MonkeyPatch, value: str, skipped: bool
) -> None:
    monkeypatch.setenv("K5_STAGE_ONE_JOINED_ANALYTICS_PHYSICAL", value)
    assert _module().pytestmark.args == (skipped,)


@pytest.mark.parametrize("accepted", [True, False])
def test_joined_receipt_is_written_only_after_this_launch_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, accepted: bool
) -> None:
    module = _module()
    output = tmp_path / "witness.json"
    output.write_text("stale receipt", encoding="utf-8")
    monkeypatch.setenv("K5_STAGE_ONE_JOINED_ANALYTICS_OUTPUT", str(output))
    monkeypatch.setenv("K5_ANALYTICS_EVIDENCE_ROOT", str(tmp_path))
    monkeypatch.setenv("K5_STAGE_ONE_REVISION", _REVISION)
    monkeypatch.setenv("ANALYTICS_LAB_SHA", _ANALYTICS_REVISION)
    provider = SimpleNamespace(provider_calls=2, tracked_detections=1 if accepted else 0)
    monkeypatch.setattr(module, "_JoinedAnalyticsProvider", lambda _root: provider)

    class Launcher:
        def __init__(self, *, delivery_factory: object, detection_provider: object) -> None:
            assert callable(delivery_factory)
            assert detection_provider is provider

        async def run(self, _source: object, *, width: int, height: int) -> SimpleNamespace:
            assert (width, height) == (1280, 720)
            return _metrics()

    monkeypatch.setattr(module, "WindowsSingleLiveOperatorLauncher", Launcher)
    if accepted:
        module.test_reviewed_video_detector_tracker_overlay_reaches_windows_operator()
        assert json.loads(output.read_text(encoding="utf-8")) == _evidence(_metrics())
    else:
        with pytest.raises(ValueError, match="positive bounded counters"):
            module.test_reviewed_video_detector_tracker_overlay_reaches_windows_operator()
        assert not output.exists()


def test_joined_workflow_uses_step_scoped_opt_in_and_preserves_reviewed_pins() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    steps = text.split("      - name: ")
    joined = next(step for step in steps if step.startswith("Prove joined detector"))
    assert text.count('K5_STAGE_ONE_JOINED_ANALYTICS_PHYSICAL: "1"') == 1
    assert 'K5_STAGE_ONE_JOINED_ANALYTICS_PHYSICAL: "1"' in joined
    assert f"run: pytest {_TEST} -q --no-cov --tb=no --show-capture=no" in joined
    assert "secrets." not in joined
    assert "continue-on-error" not in joined
    assert f"ANALYTICS_LAB_SHA: {_ANALYTICS_REVISION}" in text
    assert '"openvino==2026.3.1" "opencv-python-headless==4.12.0.88"' in text
    assert "K5_STAGE_ONE_REVISION: ${{ github.sha }}" in text
    assert "group: stage-one-operator-physical" in text


def test_joined_workflow_validates_both_receipts_before_explicit_upload() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    validation = text.split("      - name: Assert retained witness is source-free\n")[1]
    assert "foreach ($output in @($env:K5_STAGE_ONE_OUTPUT, " in validation
    assert (
        "$env:K5_STAGE_ONE_JOINED_ANALYTICS_OUTPUT, $env:K5_ALPHA_DIRECT_RTSP_OUTPUT))"
        in validation
    )
    assert "Test-Path -LiteralPath $output" in validation
    assert "Get-Content -LiteralPath $output -Raw" in validation
    upload = text.split("      - name: Upload source-free Stage One witness\n")[1]
    assert "if: success() && steps.safe_evidence.outcome == 'success'" in upload
    assert "path: |\n            artifacts/stage-one-operator-physical.json\n" in upload
    assert "            artifacts/stage-one-joined-analytics-physical.json\n" in upload
    assert "if-no-files-found: error" in upload
    assert "*" not in upload
