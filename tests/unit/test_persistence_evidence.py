from __future__ import annotations

import pytest
from pydantic import ValidationError

from k5vision.media.file_sink import FileSinkState
from k5vision.media.persistence_evidence import LocalPersistenceEvidence


def _evidence(**overrides) -> LocalPersistenceEvidence:
    values = {
        "revision": "a" * 40,
        "execution_context": "camera-lab-windows-x64",
        "delivered_packets": 32,
        "delivered_bytes": 4096,
        "persisted_bytes": 4096,
        "sink_state": FileSinkState.FINALIZED,
        "cleanup_confirmed": True,
        "elapsed_ms": 1000,
    }
    values.update(overrides)
    return LocalPersistenceEvidence(**values)


def test_persistence_evidence_accepts_exact_source_free_success() -> None:
    evidence = _evidence()
    assert evidence.accepted
    payload = evidence.model_dump_json()
    assert "rtsp://" not in payload.casefold()
    assert "credential" not in payload.casefold()
    assert "runner" not in payload.casefold()
    assert "\\\\" not in payload


@pytest.mark.parametrize("value", ["192.0.2.1", "2001:db8::1"])
def test_execution_context_rejects_literal_network_identity(value: str) -> None:
    with pytest.raises(ValidationError):
        _evidence(execution_context=value)


def test_persistence_evidence_requires_cleanup_and_matching_bytes() -> None:
    assert not _evidence(cleanup_confirmed=False).accepted
    assert not _evidence(persisted_bytes=4095).accepted
    assert not _evidence(sink_state=FileSinkState.ABORTED).accepted
