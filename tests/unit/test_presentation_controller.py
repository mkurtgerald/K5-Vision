from __future__ import annotations

import asyncio

import pytest

from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
from k5vision.media.mixed_presentation import (
    BoundedMixedPresentation,
    MixedLiveStream,
    MixedPlaybackStream,
)
from k5vision.media.presentation_controller import (
    BoundedPresentationController,
    PresentationControllerError,
    PresentationControllerErrorCode,
    PresentationControllerState,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_playback import (
    PresentationPlaybackSnapshot,
    PresentationPlaybackState,
)
from k5vision.media.presentation_session import BoundedPresentationSession
from k5vision.media.viewport_dispatch import BoundedViewportDispatcher, ViewportBinding


def _frame(source_elapsed_ms: int) -> PresentationVideoFrame:
    return PresentationVideoFrame(
        payload=memoryview(b"abcd"),
        width=1,
        height=1,
        stride_bytes=4,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=source_elapsed_ms,
    )


class FakeLiveDelivery:
    def __init__(self, *, block: bool = False, fail: bool = False) -> None:
        self.block = block
        self.fail = fail

    async def run(self, source_uri: str, consumer: object) -> LivePresentationSnapshot:
        if self.fail:
            raise RuntimeError(f"SECRET {source_uri}")
        if self.block:
            await asyncio.Event().wait()
        await consumer(_frame(10))  # type: ignore[operator]
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=1,
            rtp_valid_packets=1,
            rtp_invalid_packets=0,
            rtp_delivered_bytes=13,
            delivered_frames=1,
            delivered_frame_bytes=4,
            source_span_ms=10,
        )


class FakePlaybackDelivery:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block

    async def run(self, consumer: object) -> PresentationPlaybackSnapshot:
        if self.block:
            await asyncio.Event().wait()
        await consumer(_frame(20))  # type: ignore[operator]
        return PresentationPlaybackSnapshot(
            state=PresentationPlaybackState.COMPLETE,
            decoder_initialized=True,
            delivered_frames=1,
            delivered_bytes=4,
            source_span_ms=20,
            pump_delivered_packets=1,
            late_packets=0,
            descriptor_verified=True,
        )


def _streams(*, block: bool = False, fail: bool = False) -> list[object]:
    return [
        MixedLiveStream(
            0,
            "rtsp://user:SECRET@192.168.1.10/live",
            FakeLiveDelivery(block=block, fail=fail),
        ),
        MixedPlaybackStream(1, FakePlaybackDelivery(block=block)),
    ]


def _controller() -> tuple[BoundedPresentationController, BoundedViewportDispatcher]:
    async def consume(_frame: PresentationVideoFrame) -> None:
        await asyncio.sleep(0)

    dispatcher = BoundedViewportDispatcher(
        [ViewportBinding(0, consume), ViewportBinding(1, consume)]
    )
    session = BoundedPresentationSession(BoundedMixedPresentation(), dispatcher)
    return BoundedPresentationController(session, stop_timeout_seconds=1.0), dispatcher


def test_start_wait_completes_and_snapshot_is_source_free() -> None:
    controller, _dispatcher = _controller()

    async def exercise() -> None:
        started = await controller.start(_streams())  # type: ignore[arg-type]
        assert started.state == PresentationControllerState.RUNNING
        final = await controller.wait()
        assert final.state == PresentationControllerState.COMPLETE
        assert final.stream_count == 2
        assert final.live_streams == 1
        assert final.playback_streams == 1
        assert final.viewport_count == 2
        assert final.completed_streams == 2
        assert final.delivered_frames == 2
        assert final.delivered_frame_bytes == 8
        payload = final.model_dump_json()
        assert "rtsp://" not in payload
        assert "192.168" not in payload
        assert "SECRET" not in payload
        assert "source_id" not in payload
        assert "recording_id" not in payload
        assert "abcd" not in payload

    asyncio.run(exercise())


def test_stop_cancels_running_session_and_releases_resources() -> None:
    controller, dispatcher = _controller()

    async def exercise() -> None:
        await controller.start(_streams(block=True))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        stopped = await controller.stop()
        assert stopped.state == PresentationControllerState.STOPPED
        assert (await controller.wait()).state == PresentationControllerState.STOPPED

    asyncio.run(exercise())
    assert dispatcher.snapshot.state.value == "closed"


def test_close_before_start_is_terminal_and_start_reuse_is_rejected() -> None:
    controller, _dispatcher = _controller()

    async def exercise() -> None:
        assert (await controller.close()).state == PresentationControllerState.CLOSED
        assert (await controller.close()).state == PresentationControllerState.CLOSED
        with pytest.raises(PresentationControllerError) as exc:
            await controller.start(_streams())  # type: ignore[arg-type]
        assert exc.value.code == PresentationControllerErrorCode.INVALID_STATE

    asyncio.run(exercise())


def test_completed_controller_rejects_reuse() -> None:
    controller, _dispatcher = _controller()

    async def exercise() -> None:
        await controller.start(_streams())  # type: ignore[arg-type]
        assert (await controller.wait()).state == PresentationControllerState.COMPLETE
        with pytest.raises(PresentationControllerError) as exc:
            await controller.start(_streams())  # type: ignore[arg-type]
        assert exc.value.code == PresentationControllerErrorCode.INVALID_STATE
        assert (await controller.close()).state == PresentationControllerState.CLOSED

    asyncio.run(exercise())


def test_session_failure_is_sanitized() -> None:
    controller, _dispatcher = _controller()

    async def exercise() -> None:
        await controller.start(_streams(fail=True))  # type: ignore[arg-type]
        with pytest.raises(PresentationControllerError) as exc:
            await controller.wait()
        assert exc.value.code == PresentationControllerErrorCode.SESSION_FAILURE
        assert "SECRET" not in str(exc.value)
        assert controller.snapshot.state == PresentationControllerState.FAILED

    asyncio.run(exercise())


def test_constructor_validates_session_and_stop_timeout() -> None:
    with pytest.raises(TypeError):
        BoundedPresentationController(object())  # type: ignore[arg-type]

    controller, _dispatcher = _controller()
    session = controller._session  # noqa: SLF001 - constructor-bound test fixture
    with pytest.raises(ValueError):
        BoundedPresentationController(session, stop_timeout_seconds=0.01)
    with pytest.raises(ValueError):
        BoundedPresentationController(session, stop_timeout_seconds=31.0)
