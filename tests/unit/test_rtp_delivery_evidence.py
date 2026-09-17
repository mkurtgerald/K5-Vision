from __future__ import annotations

import pytest
from pydantic import ValidationError

from k5vision.media.rtp_delivery_evidence import RtpDeliveryEvidence


def test_delivery_evidence_is_source_free_and_accepted() -> None:
    evidence = RtpDeliveryEvidence(
        revision="a" * 40,
        execution_context="camera-lab-windows-x64",
        valid_packets=16,
        invalid_packets=0,
        delivered_bytes=20480,
        consumer_callbacks=16,
        elapsed_ms=750,
    )

    assert evidence.accepted
    payload = evidence.model_dump_json()
    assert "rtsp://" not in payload
    assert "credential" not in payload.casefold()
    assert "runner" not in payload.casefold()


def test_delivery_evidence_rejects_literal_network_identity() -> None:
    with pytest.raises(ValidationError):
        RtpDeliveryEvidence(
            revision="b" * 40,
            execution_context="192.0.2.10",
            valid_packets=1,
            invalid_packets=0,
            delivered_bytes=1200,
            consumer_callbacks=1,
            elapsed_ms=10,
        )


def test_callback_mismatch_is_not_accepted() -> None:
    evidence = RtpDeliveryEvidence(
        revision="c" * 40,
        execution_context="camera-lab-windows-x64",
        valid_packets=2,
        invalid_packets=0,
        delivered_bytes=2400,
        consumer_callbacks=1,
        elapsed_ms=10,
    )
    assert not evidence.accepted
