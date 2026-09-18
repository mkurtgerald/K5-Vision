from __future__ import annotations

import asyncio

import pytest

from k5vision.media.live_presentation import (
    LivePresentationError,
    LivePresentationErrorCode,
    LivePresentationSnapshot,
    LivePresentationState,
)
from k5vision.media.mixed_presentation import (
    BoundedMixedPresentation,
    MixedLiveStream,
    MixedPlaybackStream,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_playback import (
    PresentationPlaybackSnapshot,
    PresentationPlaybackState,
)
from k5vision.media.presentation_session import (
    BoundedPresentationSession,
    PresentationSessionError,
    PresentationSessionErrorCode,
    PresentationSessionState,
)
from k5vision.media.viewport_dispatch import (
    BoundedViewportDispatcher,
    ViewportBinding,
    ViewportDispatchState,
)


def _frame(source_elapsed_ms: int, payload: bytes = b"abcd") -> PresentationVideoFrame:
    return PresentationVideoFrame(
        payload=memoryview(payload),
        width=1,
        height=1,
        stride_bytes=4,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=source_elapsed_ms,
    )


class FakeLiveDelivery:
    def __init__(
        self,
        frames: list[PresentationVideoFrame],
        *,
        fail: bool = False,
        block: bool = False,
    ) -> None:
        self.frames = frames
        self.fail = fail
        self.block = block

    async def run(self, source_uri: str, consumer: object) -> LivePresentationSnapshot:
        if self.fail:
            raise LivePresentationError(
                LivePresentationErrorCode.DELIVERY_FAILURE,
                f"SECRET source detail {source_uri}",
            )
        if self.block:
            await asyncio.Event().wait()
        for frame in self.frames:
            await consumer(frame)  # type: ignore[operator]
            await asyncio.sleep(0)
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=max(1, len(self.frames)),
            rtp_valid_packets=max(1, len(self.frames)),
            rtp_invalid_packets=0,
            rtp_delivered_bytes=max(1, len(self.frames) * 13),
            delivered_frames=len(self.frames),
            delivered_frame_bytes=sum(len(frame.payload) for frame in self.frames),
            source_span_ms=max((frame.source_elapsed_ms for frame in self.frames), default=0),
        )


class FakePlaybackDelivery:
    def __init__(
        self,
        frames: list[PresentationVideoFrame],
        *,
        block: bool = False,
    ) -> None:
        self.frames = frames
        self.block = block

    async def run(self, consumer: object) -> PresentationPlaybackSnapshot:
        if self.block:
            await asyncio.Event().wait()
        for frame in self.frames:
            await consumer(frame)  # type: ignore[operator]
            await asyncio.sleep(0)
        return PresentationPlaybackSnapshot(
            state=PresentationPlaybackState.COMPLETE,
            decoder_initialized=True,
            delivered_frames=len(self.frames),
            delivered_bytes=sum(len(frame.payload) for frame in self.frames),
            source_span_ms=max((frame.source_elapsed_ms for frame in self.frames), default=0),
            pump_delivered_packets=max(1, len(self.frames)),
            late_packets=0,
            descriptor_verified=True,
        )


def _streams(
    *,
    live: FakeLiveDelivery | None = None,
    playback: FakePlaybackDelivery | None = None,
) -> list[MixedLiveStream | MixedPlaybackStream]:
    return [
        MixedLiveStream(
            0,
            "rtsp://user:SECRET@192.168.1.10/live",
            live or FakeLiveDelivery([_frame(10)]),
        ),
        MixedPlaybackStream(1, playback or FakePlaybackDelivery([_frame(20)])),
    ]


def _dispatcher(
    first: object | None = None,
    second: object | None = None,
) -> BoundedViewportDispatcher:
    async def default(_frame: PresentationVideoFrame) -> None:
        return None

    return BoundedViewportDispatcher(
        [
            ViewportBinding(0, first or default),  # type: ignore[arg-type]
            ViewportBinding(1, second or default),  # type: ignore[arg-type]
        ]
    )


def test_session_composes_mixed_presentation_and_releases_dispatcher() -> None:
    observed = {0: 0, 1: 0}

    async def first(_frame: PresentationVideoFrame) -> None:
        observed[0] += 1

    async def second(_frame: PresentationVideoFrame) -> None:
        observed[1] += 1

    dispatcher = _dispatcher(first, second)
    session = BoundedPresentationSession(BoundedMixedPresentation(), dispatcher)
    snapshot = asyncio.run(session.run(_streams()))

    assert observed == {0: 1, 1: 1}
    assert snapshot.state == PresentationSessionState.COMPLETE
    assert snapshot.stream_count == 2
    assert snapshot.live_streams == 1
    assert snapshot.playback_streams == 1
    assert snapshot.viewport_count == 2
    assert snapshot.completed_streams == 2
    assert snapshot.delivered_frames == 2
    assert snapshot.delivered_frame_bytes == 8
    assert snapshot.max_source_span_ms == 20
    assert dispatcher.snapshot.state == ViewportDispatchState.CLOSED
    payload = snapshot.model_dump_json()
    assert "rtsp://" not in payload
    assert "192.168" not in payload
    assert "recording_id" not in payload
    assert "source_id" not in payload
    assert "SECRET" not in payload
    assert "abcd" not in payload


def test_child_failure_is_sanitized_and_dispatcher_is_released() -> None:
    dispatcher = _dispatcher()
    session = BoundedPresentationSession(BoundedMixedPresentation(), dispatcher)
    with pytest.raises(PresentationSessionError) as exc:
        asyncio.run(session.run(_streams(live=FakeLiveDelivery([], fail=True))))
    assert exc.value.code == PresentationSessionErrorCode.PRESENTATION_FAILURE
    assert "SECRET" not in str(exc.value)
    assert session.snapshot.state == PresentationSessionState.FAILED
    assert dispatcher.snapshot.state == ViewportDispatchState.CLOSED


def test_viewport_failure_is_sanitized_and_remains_fail_closed() -> None:
    async def fail(_frame: PresentationVideoFrame) -> None:
        raise RuntimeError("SECRET renderer detail")

    dispatcher = _dispatcher(fail)
    session = BoundedPresentationSession(BoundedMixedPresentation(), dispatcher)
    with pytest.raises(PresentationSessionError) as exc:
        asyncio.run(session.run(_streams()))
    assert exc.value.code == PresentationSessionErrorCode.PRESENTATION_FAILURE
    assert "SECRET" not in str(exc.value)
    assert session.snapshot.state == PresentationSessionState.FAILED
    assert dispatcher.snapshot.state == ViewportDispatchState.FAILED


def test_cancellation_releases_dispatcher_and_sets_terminal_state() -> None:
    dispatcher = _dispatcher()
    session = BoundedPresentationSession(BoundedMixedPresentation(), dispatcher)

    async def exercise() -> None:
        task = asyncio.create_task(
            session.run(
                _streams(
                    live=FakeLiveDelivery([], block=True),
                    playback=FakePlaybackDelivery([], block=True),
                )
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert session.snapshot.state == PresentationSessionState.CANCELLED
    assert dispatcher.snapshot.state == ViewportDispatchState.CLOSED


def test_pre_run_close_is_terminal_and_completed_session_rejects_reuse() -> None:
    closed_dispatcher = _dispatcher()
    closed_session = BoundedPresentationSession(
        BoundedMixedPresentation(),
        closed_dispatcher,
    )
    snapshot = asyncio.run(closed_session.close())
    assert snapshot.state == PresentationSessionState.CLOSED
    assert closed_dispatcher.snapshot.state == ViewportDispatchState.CLOSED
    with pytest.raises(PresentationSessionError) as closed_exc:
        asyncio.run(closed_session.run(_streams()))
    assert closed_exc.value.code == PresentationSessionErrorCode.INVALID_STATE

    dispatcher = _dispatcher()
    completed = BoundedPresentationSession(BoundedMixedPresentation(), dispatcher)
    asyncio.run(completed.run(_streams()))
    with pytest.raises(PresentationSessionError) as reuse_exc:
        asyncio.run(completed.run(_streams()))
    assert reuse_exc.value.code == PresentationSessionErrorCode.INVALID_STATE
    assert asyncio.run(completed.close()).state == PresentationSessionState.COMPLETE


def test_constructor_requires_accepted_boundaries() -> None:
    dispatcher = _dispatcher()
    with pytest.raises(TypeError):
        BoundedPresentationSession(object(), dispatcher)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        BoundedPresentationSession(BoundedMixedPresentation(), object())  # type: ignore[arg-type]
