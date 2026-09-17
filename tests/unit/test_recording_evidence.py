from __future__ import annotations

import pytest
from pydantic import ValidationError

from k5vision.media.recording import RecordingState
from k5vision.media.recording_evidence import RecordingIngestEvidence


def evidence(**changes: object) -> RecordingIngestEvidence:
    values: dict[str, object] = {
        "revision": "a" * 40,
        "execution_context": "camera-lab-windows-x64",
        "delivered_packets": 32,
        "delivered_bytes": 4096,
        "sink_packets": 32,
        "sink_bytes": 4096,
        "final_state": RecordingState.FINALIZED,
        "elapsed_ms": 1200,
    }
    values.update(changes)
    return RecordingIngestEvidence(**values)


def test_recording_evidence_accepts_matching_source_free_counts() -> None:
    item = evidence()
    assert item.accepted
    payload = item.model_dump_json().casefold()
    assert "://" not in payload
    assert "username" not in payload
    assert "password" not in payload
    assert "credential" not in payload
    assert "runner" not in payload


def test_recording_evidence_rejects_literal_network_identity() -> None:
    with pytest.raises(ValidationError):
        evidence(execution_context="192.0.2.10")


def test_recording_evidence_requires_exact_delivery_sink_match() -> None:
    assert not evidence(sink_packets=31).accepted
    assert not evidence(sink_bytes=4095).accepted
    assert not evidence(final_state=RecordingState.ABORTED).accepted
