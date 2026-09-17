from __future__ import annotations

import pytest
from pydantic import ValidationError

from k5vision.media.session import MediaSessionState
from k5vision.media.stage04_evidence import Stage04PhysicalEvidence


def valid_evidence(**updates: object) -> Stage04PhysicalEvidence:
    values: dict[str, object] = {
        "revision": "a" * 40,
        "receive_probe_passed": True,
        "lifecycle_states": (
            MediaSessionState.RUNNING,
            MediaSessionState.STOPPED,
            MediaSessionState.RUNNING,
            MediaSessionState.CLOSED,
        ),
        "generation_count": 2,
        "final_state": MediaSessionState.CLOSED,
    }
    values.update(updates)
    return Stage04PhysicalEvidence.model_validate(values)


def test_stage04_physical_evidence_is_source_free_and_versioned() -> None:
    evidence = valid_evidence()
    payload = evidence.model_dump_json()
    assert evidence.schema_version == "1"
    assert evidence.runtime == "GStreamer 1.28.7"
    assert evidence.transport == "udp"
    assert "rtsp://" not in payload
    assert "credential" not in payload.casefold()
    assert "runner" not in payload.casefold()


def test_stage04_physical_evidence_accepts_local_revision_for_local_validation() -> None:
    assert valid_evidence(revision="local").revision == "local"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("receive_probe_passed", False),
        ("generation_count", 1),
        ("final_state", MediaSessionState.STOPPED),
        ("revision", "not-a-revision"),
    ],
)
def test_stage04_physical_evidence_rejects_incomplete_success(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        valid_evidence(**{field: value})


def test_stage04_physical_evidence_requires_complete_lifecycle() -> None:
    with pytest.raises(ValidationError):
        valid_evidence(
            lifecycle_states=(
                MediaSessionState.RUNNING,
                MediaSessionState.RUNNING,
                MediaSessionState.RUNNING,
                MediaSessionState.CLOSED,
            )
        )
