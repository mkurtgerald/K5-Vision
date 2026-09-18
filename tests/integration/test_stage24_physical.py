"""Physical qualification for bounded recording-to-presentation playback delivery."""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink, FramedRecordingState
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_playback import BoundedPresentationPlaybackDelivery
from k5vision.media.presentation_playback_evidence import (
    PresentationPlaybackPhysicalEvidence,
    write_evidence,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE24_PHYSICAL") != "1",
    reason="Stage 24 physical qualification is opt-in",
)


def _physical_context() -> tuple[str, str, Path, str, Path]:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE24_OUTPUT"])
    revision = os.environ["K5_STAGE24_REVISION"].casefold()
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    authenticated_source = selected_source_uri(source, credentials, credential_index)
    return source, authenticated_source, output, revision, runner_temp


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


def _rtp_timestamp(packet: memoryview) -> int:
    return int.from_bytes(packet[4:8], "big")


def test_stage24_live_recording_replays_into_presentation_consumer() -> None:
    source, authenticated_source, output, revision, runner_temp = _physical_context()
    scratch = runner_temp / f"k5-stage24-{uuid4().hex}"

    async def qualify() -> PresentationPlaybackPhysicalEvidence:
        recording_id = uuid4()
        source_id = uuid4()
        sink = FramedAtomicRecordingSink(scratch, str(recording_id), max_packets=2048)
        first_timestamp: int | None = None
        last_timestamp: int | None = None
        payload_type: int | None = None
        playback_snapshot = None
        geometry: tuple[int, int, int] | None = None

        await sink.open()

        async def record(packet: memoryview) -> None:
            nonlocal first_timestamp, last_timestamp, payload_type
            timestamp = _rtp_timestamp(packet)
            if first_timestamp is None:
                first_timestamp = timestamp
                payload_type = int(packet[1] & 0x7F)
            last_timestamp = timestamp
            await sink.write(packet)

        delivery = EphemeralRtpDelivery(
            packet_goal=2048,
            delivery_timeout_seconds=30.0,
            consumer_timeout_seconds=3.0,
            relay_startup_probe_seconds=0.5,
        )

        try:
            await delivery.deliver(authenticated_source, record)
            await sink.finalize()
            assert first_timestamp is not None
            assert last_timestamp is not None
            assert payload_type is not None

            duration_ms = (((last_timestamp - first_timestamp) & 0xFFFFFFFF) * 1000) // 90_000
            started = datetime.now(UTC).replace(microsecond=0)
            ended = started + timedelta(milliseconds=duration_ms)
            counters = sink.snapshot
            descriptor = RecordingStreamDescriptor(
                recording_id=recording_id,
                source_id=source_id,
                codec=VideoCodec.H264,
                payload_type=payload_type,
                clock_rate_hz=90_000,
                started_at_utc=started,
                ended_at_utc=ended,
                duration_ms=duration_ms,
                rtp_timestamp_origin=first_timestamp,
                packet_count=counters.packets,
                payload_bytes=counters.payload_bytes,
                file_bytes=counters.file_bytes,
            )

            async def present(frame: PresentationVideoFrame) -> None:
                nonlocal geometry
                current = (frame.width, frame.height, frame.stride_bytes)
                if geometry is None:
                    geometry = current
                assert current == geometry
                assert frame.pixel_format == PixelFormat.BGRX
                assert len(frame.payload) == frame.stride_bytes * frame.height

            playback = BoundedPresentationPlaybackDelivery(
                scratch / f"{recording_id}.k5r",
                descriptor,
                0,
                duration_ms,
                PlaybackRate.SIXTEEN,
                decoder_timeout_seconds=2.0,
                frame_consumer_timeout_seconds=2.0,
                pump_consumer_timeout_seconds=4.0,
            )
            playback_snapshot = await playback.run(present)

            assert playback_snapshot.delivered_frames >= 1
            assert playback_snapshot.delivered_bytes >= playback_snapshot.delivered_frames
            assert playback_snapshot.descriptor_verified is True
            assert geometry is not None
            width, height, stride_bytes = geometry
            return PresentationPlaybackPhysicalEvidence(
                revision=revision,
                execution_context="camera-lab-windows-x64",
                recorded_packets=counters.packets,
                recorded_bytes=counters.payload_bytes,
                delivered_frames=playback_snapshot.delivered_frames,
                delivered_bytes=playback_snapshot.delivered_bytes,
                width=width,
                height=height,
                stride_bytes=stride_bytes,
                source_span_ms=playback_snapshot.source_span_ms,
                final_state=playback_snapshot.state,
            )
        finally:
            if sink.snapshot.state not in {
                FramedRecordingState.FINALIZED,
                FramedRecordingState.ABORTED,
            }:
                try:
                    await sink.abort()
                except Exception:
                    pass
            shutil.rmtree(scratch, ignore_errors=True)

    evidence = asyncio.run(qualify())
    assert not scratch.exists()
    payload = evidence.model_dump_json(indent=2) + "\n"
    _assert_source_free(payload, source, authenticated_source)
    write_evidence(output, evidence)
