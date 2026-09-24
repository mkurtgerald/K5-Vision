"""Hosted regression for source-free Stage-One physical failure localization."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _physical_witness_module() -> ModuleType:
    path = Path("tests/integration/test_stage_one_operator_physical.py")
    spec = importlib.util.spec_from_file_location("k5_stage_one_physical_witness", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_physical_analytics_failure_diagnostic_keeps_only_bounded_aggregates() -> None:
    module = _physical_witness_module()
    receipt = {
        "analytics_enabled": True,
        "analytics_provider_submissions": 3,
        "analytics_provider_completions": 2,
        "analytics_failures": 1,
        "analytics_rendered_boxes": 0,
    }

    diagnostic = module._source_free_analytics_diagnostic(
        receipt,
        provider_calls=4,
        tracked_detections=2,
    )

    assert diagnostic == (
        "enabled=true;provider_calls=4;tracked_detections=2;submissions=3;"
        "completions=2;failures=1;rendered_boxes=0"
    )
    assert module._analytics_acceptance_met(receipt) is False


def test_physical_analytics_diagnostic_never_echoes_untrusted_receipt_values() -> None:
    module = _physical_witness_module()
    secret = "rtsp://operator:private-secret@192.168.1.200/live"
    receipt = {
        "analytics_enabled": secret,
        "analytics_provider_submissions": secret,
        "analytics_provider_completions": True,
        "analytics_failures": -1,
        "analytics_rendered_boxes": 1_000_001,
    }

    diagnostic = module._source_free_analytics_diagnostic(
        receipt,
        provider_calls=secret,
        tracked_detections=secret,
    )

    assert secret not in diagnostic
    assert "rtsp://" not in diagnostic
    assert "192.168." not in diagnostic
    assert diagnostic == (
        "enabled=invalid;provider_calls=invalid;tracked_detections=invalid;"
        "submissions=invalid;completions=invalid;failures=invalid;rendered_boxes=invalid"
    )
    assert module._analytics_acceptance_met(receipt) is False


def test_physical_analytics_acceptance_requires_clean_completed_rendering() -> None:
    module = _physical_witness_module()
    receipt = {
        "analytics_enabled": True,
        "analytics_provider_submissions": 2,
        "analytics_provider_completions": 2,
        "analytics_failures": 0,
        "analytics_rendered_boxes": 1,
    }

    assert module._analytics_acceptance_met(receipt) is True
