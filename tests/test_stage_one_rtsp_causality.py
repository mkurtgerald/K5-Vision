"""Regression tests for impossible Stage-One RTSP receipt counter sequences."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_stage_one_rtsp_witness.py"
_REVISION = "a" * 40
_ANALYTICS_REVISION = "b" * 40


def _validator():
    spec = importlib.util.spec_from_file_location("stage_one_rtsp_causality", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_receipt


def _receipt() -> dict[str, object]:
    return {
        "schema_version": "1",
        "revision": _REVISION,
        "analytics_revision": _ANALYTICS_REVISION,
        "execution_context": "reviewed-video-loopback-rtsp-windows-x64",
        "analytics_failures": 0,
        "windows_live_launch_completed": True,
        "analytics_enabled": True,
        "rtsp_tcp_joined": True,
        "delivered_frames": 1,
        "presentations": 1,
        "analytics_provider_calls": 1,
        "analytics_tracked_detections": 1,
        "analytics_provider_submissions": 1,
        "analytics_provider_completions": 1,
        "analytics_rendered_boxes": 1,
    }


def _validate(tmp_path: Path, document: dict[str, object]) -> None:
    path = tmp_path / "generated-scalar-receipt.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    _validator()(path, revision=_REVISION, analytics_revision=_ANALYTICS_REVISION)


def test_allows_one_completion_pending_at_cleanup(tmp_path: Path) -> None:
    document = {
        **_receipt(),
        "delivered_frames": 60,
        "presentations": 60,
        "analytics_provider_submissions": 60,
        "analytics_provider_calls": 60,
        "analytics_provider_completions": 59,
    }
    _validate(tmp_path, document)


@pytest.mark.parametrize(
    "counter",
    ["analytics_provider_completions", "analytics_provider_calls", "analytics_provider_submissions"],
)
def test_rejects_impossible_analytics_counters(tmp_path: Path, counter: str) -> None:
    with pytest.raises(ValueError, match="^receipt analytics counters are causally inconsistent$"):
        _validate(tmp_path, {**_receipt(), counter: 2})
