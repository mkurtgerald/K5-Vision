"""Physical qualification for bounded live RTP-to-presentation delivery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from k5vision.media.live_presentation import BoundedLivePresentationDelivery
from k5vision.media.live_presentation_evidence import (
    LivePresentationPhysicalEvidence,
    write_evidence,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE25_PHYSICAL") != "1",
    reason="Stage 25 physical qualification is opt-in",
)


def _physical_context() -> tuple[str, str, Path, str]:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE25_OUTPUT"])
    revision = os.environ["K5_STAGE25_REVISION"].casefold()
    authenticated_source = selected_source_uri(source, credentials, credential_index)
    return source, authenticated_source, output, revision


def _assert_source_free(payload: str, source: str, authenticated_source: str) -> None:
    assert source not in payload
    assert authenticated_source not in payload
    lowered = payload.casefold()
    for token in (
        "rtsp://",
        "credential",
        "password",
        "runner_name",
        "payload",
        "recording_id",
        "source_id",
    ):
        assert token not in lowered


def test_stage25_live_rtp_decodes_into_presentation_consumer() -> None:
    source, authenticated_source, output, revision = _physical_context()

    async def qualify() -> LivePresentationPhysicalEvidence:
        payload_type: int | None = None

        async def probe(packet: memoryview) -> None:
            nonlocal payload_type
            payload_type = int(packet[1] & 0x7F)

        probe_delivery = EphemeralRtpDelivery(
            packet_goal=1,
            delivery_timeout_seconds=15.0,
            consumer_timeout_seconds=2.0,
            relay_startup_probe_seconds=0.5,
        )
        await probe_delivery.deliver(authenticated_source, probe)
        assert payload_type is not None
        assert 96 <= payload_type <= 127

        geometry: tuple[int, int, int] | None = None

        async def present(frame: PresentationVideoFrame) -> None:
            nonlocal geometry
            current = (frame.width, frame.height, frame.stride_bytes)
            if geometry is None:
                geometry = current
            assert current == geometry
            assert frame.pixel_format == PixelFormat.BGRX
            assert len(frame.payload) == frame.stride_bytes * frame.height

        delivery = BoundedLivePresentationDelivery(
            payload_type,
            packet_goal=2048,
            delivery_timeout_seconds=30.0,
            packet_consumer_timeout_seconds=4.0,
            decoder_timeout_seconds=2.0,
            frame_consumer_timeout_seconds=2.0,
            cleanup_timeout_seconds=2.0,
            relay_startup_probe_seconds=0.5,
        )
        snapshot = await delivery.run(authenticated_source, present)

        assert snapshot.accepted_packets >= 1
        assert snapshot.rtp_valid_packets == snapshot.accepted_packets
        assert snapshot.delivered_frames >= 1
        assert snapshot.delivered_frame_bytes >= snapshot.delivered_frames
        assert geometry is not None
        width, height, stride_bytes = geometry
        return LivePresentationPhysicalEvidence(
            revision=revision,
            execution_context="camera-lab-windows-x64",
            accepted_packets=snapshot.accepted_packets,
            rtp_delivered_bytes=snapshot.rtp_delivered_bytes,
            delivered_frames=snapshot.delivered_frames,
            delivered_frame_bytes=snapshot.delivered_frame_bytes,
            width=width,
            height=height,
            stride_bytes=stride_bytes,
            source_span_ms=snapshot.source_span_ms,
            final_state=snapshot.state,
        )

    evidence = asyncio.run(qualify())
    payload = evidence.model_dump_json(indent=2) + "\n"
    _assert_source_free(payload, source, authenticated_source)
    write_evidence(output, evidence)
