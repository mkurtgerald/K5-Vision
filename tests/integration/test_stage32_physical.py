"""Physical qualification for the bounded operator presentation runtime assembly."""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink, FramedRecordingState
from k5vision.media.live_presentation import BoundedLivePresentationDelivery
from k5vision.media.mixed_presentation import MixedLiveStream, MixedPlaybackStream
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_playback import BoundedPresentationPlaybackDelivery
from k5vision.media.presentation_runtime import (
    BoundedPresentationRuntime,
    PresentationRuntimeState,
)
from k5vision.media.presentation_runtime_evidence import (
    PresentationRuntimePhysicalEvidence,
    write_evidence,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE32_PHYSICAL") != "1",
    reason="Stage 32 physical qualification is opt-in",
)


def _physical_context() -> tuple[str, str, Path, str, Path]:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE32_OUTPUT"])
    revision = os.environ["K5_STAGE32_REVISION"].casefold()
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


def test_stage32_runtime_assembles_mixed_live_playback_to_viewports() -> None:
    source, authenticated_source, output, revision, runner_temp = _physical_context()
    scratch = runner_temp / f"k5-stage32-{uuid4().hex}"

    async def qualify() -> PresentationRuntimePhysicalEvidence:
        recording_id = uuid4()
        source_id = uuid4()
        sink = FramedAtomicRecordingSink(scratch, str(recording_id), max_packets=2048)
        first_timestamp: int | None = None
        last_timestamp: int | None = None
        payload_type: int | None = None

        await sink.open()

        async def record(packet: memoryview) -> None:
            nonlocal first_timestamp, last_timestamp, payload_type
            timestamp = _rtp_timestamp(packet)
            if first_timestamp is None:
                first_timestamp = timestamp
                payload_type = int(packet[1] & 0x7F)
            last_timestamp = timestamp
            await sink.write(packet)

        recorder = EphemeralRtpDelivery(
            packet_goal=2048,
            delivery_timeout_seconds=30.0,
            consumer_timeout_seconds=3.0,
            relay_startup_probe_seconds=0.5,
        )

        try:
            await recorder.deliver(authenticated_source, record)
            await sink.finalize()
            assert first_timestamp is not None
            assert last_timestamp is not None
            assert payload_type is not None
            assert 96 <= payload_type <= 127

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
            recording_path = scratch / f"{recording_id}.k5r"
            live_delivery = BoundedLivePresentationDelivery(
                payload_type,
                packet_goal=2048,
                delivery_timeout_seconds=30.0,
                packet_consumer_timeout_seconds=4.0,
                decoder_timeout_seconds=2.0,
                frame_consumer_timeout_seconds=2.0,
                cleanup_timeout_seconds=2.0,
                relay_startup_probe_seconds=0.5,
            )
            playback_delivery = BoundedPresentationPlaybackDelivery(
                recording_path,
                descriptor,
                0,
                duration_ms,
                PlaybackRate.SIXTEEN,
                decoder_timeout_seconds=2.0,
                frame_consumer_timeout_seconds=2.0,
                pump_consumer_timeout_seconds=4.0,
                cleanup_timeout_seconds=2.0,
            )
            frames_by_slot = {0: 0, 1: 0}
            geometry: dict[int, tuple[int, int, int]] = {}

            async def viewport_zero(frame: PresentationVideoFrame) -> None:
                current = (frame.width, frame.height, frame.stride_bytes)
                geometry.setdefault(0, current)
                assert current == geometry[0]
                assert frame.pixel_format == PixelFormat.BGRX
                assert len(frame.payload) == frame.stride_bytes * frame.height
                frames_by_slot[0] += 1
                await asyncio.sleep(0)

            async def viewport_one(frame: PresentationVideoFrame) -> None:
                current = (frame.width, frame.height, frame.stride_bytes)
                geometry.setdefault(1, current)
                assert current == geometry[1]
                assert frame.pixel_format == PixelFormat.BGRX
                assert len(frame.payload) == frame.stride_bytes * frame.height
                frames_by_slot[1] += 1
                await asyncio.sleep(0)

            runtime = BoundedPresentationRuntime(
                [ViewportBinding(0, viewport_zero), ViewportBinding(1, viewport_one)],
                max_streams=2,
                max_viewports=2,
                max_total_frames=100_000,
                max_total_frame_bytes=16 * 1024 * 1024 * 1024,
                consumer_timeout_seconds=2.0,
                stop_timeout_seconds=5.0,
            )
            streams = [
                MixedLiveStream(0, authenticated_source, live_delivery),
                MixedPlaybackStream(1, playback_delivery),
            ]
            started_snapshot = await runtime.start(streams)
            assert started_snapshot.state == PresentationRuntimeState.RUNNING
            snapshot = await runtime.wait()

            assert snapshot.state == PresentationRuntimeState.COMPLETE
            assert snapshot.stream_count == 2
            assert snapshot.live_streams == 1
            assert snapshot.playback_streams == 1
            assert snapshot.viewport_count == 2
            assert snapshot.completed_streams == 2
            assert frames_by_slot[0] >= 1
            assert frames_by_slot[1] >= 1
            assert snapshot.delivered_frames == frames_by_slot[0] + frames_by_slot[1]
            assert snapshot.delivered_frame_bytes >= snapshot.delivered_frames
            assert (await runtime.close()).state == PresentationRuntimeState.CLOSED

            return PresentationRuntimePhysicalEvidence(
                revision=revision,
                execution_context="camera-lab-windows-x64",
                stream_count=snapshot.stream_count,
                live_streams=snapshot.live_streams,
                playback_streams=snapshot.playback_streams,
                viewport_count=snapshot.viewport_count,
                completed_streams=snapshot.completed_streams,
                delivered_frames=snapshot.delivered_frames,
                delivered_frame_bytes=snapshot.delivered_frame_bytes,
                max_source_span_ms=snapshot.max_source_span_ms,
                final_state=snapshot.state,
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
