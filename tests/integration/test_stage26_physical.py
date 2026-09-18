"""Physical qualification for bounded concurrent live presentation delivery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from k5vision.media.live_presentation import BoundedLivePresentationDelivery
from k5vision.media.multiview_live_presentation import (
    BoundedMultiViewLivePresentation,
    MultiViewLiveStream,
)
from k5vision.media.multiview_live_presentation_evidence import (
    MultiViewLivePhysicalEvidence,
    write_evidence,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE26_PHYSICAL") != "1",
    reason="Stage 26 physical qualification is opt-in",
)


def _physical_context() -> tuple[str, str, Path, str]:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE26_OUTPUT"])
    revision = os.environ["K5_STAGE26_REVISION"].casefold()
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


def test_stage26_two_concurrent_live_presentations_reach_serialized_consumer() -> None:
    source, authenticated_source, output, revision = _physical_context()

    async def qualify() -> MultiViewLivePhysicalEvidence:
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

        streams = [
            MultiViewLiveStream(
                slot,
                authenticated_source,
                BoundedLivePresentationDelivery(
                    payload_type,
                    packet_goal=1024,
                    delivery_timeout_seconds=30.0,
                    packet_consumer_timeout_seconds=4.0,
                    decoder_timeout_seconds=2.0,
                    frame_consumer_timeout_seconds=2.0,
                    cleanup_timeout_seconds=2.0,
                    relay_startup_probe_seconds=0.5,
                ),
            )
            for slot in (0, 1)
        ]
        coordinator = BoundedMultiViewLivePresentation(
            max_streams=2,
            max_total_frames=100_000,
            max_total_frame_bytes=4 * 1024 * 1024 * 1024,
            consumer_timeout_seconds=2.0,
        )
        geometry: dict[int, tuple[int, int, int]] = {}
        frames_by_slot = {0: 0, 1: 0}
        active_consumers = 0
        max_active_consumers = 0

        async def present(slot: int, frame: PresentationVideoFrame) -> None:
            nonlocal active_consumers, max_active_consumers
            active_consumers += 1
            max_active_consumers = max(max_active_consumers, active_consumers)
            try:
                current = (frame.width, frame.height, frame.stride_bytes)
                if slot not in geometry:
                    geometry[slot] = current
                assert current == geometry[slot]
                assert frame.pixel_format == PixelFormat.BGRX
                assert len(frame.payload) == frame.stride_bytes * frame.height
                frames_by_slot[slot] += 1
                await asyncio.sleep(0)
            finally:
                active_consumers -= 1

        snapshot = await coordinator.run(streams, present)

        assert snapshot.stream_count == 2
        assert snapshot.completed_streams == 2
        assert frames_by_slot[0] >= 1
        assert frames_by_slot[1] >= 1
        assert max_active_consumers == 1
        assert snapshot.delivered_frames == frames_by_slot[0] + frames_by_slot[1]
        assert snapshot.delivered_frame_bytes >= snapshot.delivered_frames
        return MultiViewLivePhysicalEvidence(
            revision=revision,
            execution_context="camera-lab-windows-x64",
            stream_count=snapshot.stream_count,
            completed_streams=snapshot.completed_streams,
            delivered_frames=snapshot.delivered_frames,
            delivered_frame_bytes=snapshot.delivered_frame_bytes,
            max_source_span_ms=snapshot.max_source_span_ms,
            final_state=snapshot.state,
        )

    evidence = asyncio.run(qualify())
    payload = evidence.model_dump_json(indent=2) + "\n"
    _assert_source_free(payload, source, authenticated_source)
    write_evidence(output, evidence)
