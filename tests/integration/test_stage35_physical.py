"""Physical qualification for transient camera frames into the Win32 DIB surface."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from k5vision.media.live_presentation import BoundedLivePresentationDelivery
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.media.windows_presentation_surface import (
    BoundedWindowsPresentationSurface,
    WindowsPresentationSurfaceState,
)
from k5vision.media.windows_presentation_surface_evidence import (
    WindowsPresentationSurfacePhysicalEvidence,
    write_evidence,
)
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE35_PHYSICAL") != "1",
    reason="Stage 35 physical qualification is opt-in",
)


def _physical_context() -> tuple[str, str, Path, str]:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE35_OUTPUT"])
    revision = os.environ["K5_STAGE35_REVISION"].casefold()
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
        "handle",
        "pointer",
    ):
        assert token not in lowered


def test_stage35_live_frames_reach_private_windows_dib_surface() -> None:
    source, authenticated_source, output, revision = _physical_context()

    async def qualify() -> WindowsPresentationSurfacePhysicalEvidence:
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

        surface = BoundedWindowsPresentationSurface(
            max_frames=100_000,
            max_frame_bytes=64 * 1024 * 1024,
            max_total_frame_bytes=8 * 1024 * 1024 * 1024,
            max_surface_replacements=4,
        )
        opened = await surface.open()
        assert opened.state == WindowsPresentationSurfaceState.OPEN
        assert not opened.surface_open

        seen_geometry: tuple[int, int, int] | None = None

        async def present(frame: PresentationVideoFrame) -> None:
            nonlocal seen_geometry
            assert frame.pixel_format == PixelFormat.BGRX
            assert len(frame.payload) == frame.stride_bytes * frame.height
            current = (frame.width, frame.height, frame.stride_bytes)
            if seen_geometry is None:
                seen_geometry = current
            assert current == seen_geometry
            await surface.present(frame)

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

        try:
            delivery_snapshot = await delivery.run(authenticated_source, present)
            active = surface.snapshot
            assert active.state == WindowsPresentationSurfaceState.OPEN
            assert active.surface_open
            assert active.presented_frames >= 1
            assert active.presented_frames == delivery_snapshot.delivered_frames
            assert active.presented_frame_bytes == delivery_snapshot.delivered_frame_bytes
            assert active.width >= 1
            assert active.height >= 1
            assert active.native_stride_bytes == active.width * 4
            assert seen_geometry is not None
            assert (active.width, active.height) == seen_geometry[:2]

            closed = await surface.close()
            assert closed.state == WindowsPresentationSurfaceState.CLOSED
            assert not closed.surface_open

            return WindowsPresentationSurfacePhysicalEvidence(
                revision=revision,
                execution_context="camera-lab-windows-x64",
                width=active.width,
                height=active.height,
                native_stride_bytes=active.native_stride_bytes,
                presented_frames=active.presented_frames,
                presented_frame_bytes=active.presented_frame_bytes,
                surface_replacements=active.surface_replacements,
                max_source_span_ms=active.max_source_span_ms,
                final_state=closed.state,
            )
        finally:
            if surface.snapshot.state != WindowsPresentationSurfaceState.CLOSED:
                try:
                    await surface.close()
                except Exception:
                    pass

    evidence = asyncio.run(qualify())
    payload = evidence.model_dump_json(indent=2) + "\n"
    _assert_source_free(payload, source, authenticated_source)
    write_evidence(output, evidence)
