from __future__ import annotations

import asyncio

import pytest

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.viewport_dispatch import (
    BoundedViewportDispatcher,
    ViewportBinding,
    ViewportDispatchError,
    ViewportDispatchErrorCode,
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


def test_dispatch_routes_frames_and_retains_only_aggregate_state() -> None:
    observed: dict[int, list[int]] = {0: [], 1: []}

    async def first(frame: PresentationVideoFrame) -> None:
        observed[0].append(frame.source_elapsed_ms)

    async def second(frame: PresentationVideoFrame) -> None:
        observed[1].append(frame.source_elapsed_ms)

    dispatcher = BoundedViewportDispatcher([ViewportBinding(0, first), ViewportBinding(1, second)])

    async def exercise() -> object:
        await dispatcher.dispatch(0, _frame(10))
        await dispatcher.dispatch(1, _frame(20))
        return await dispatcher.close()

    snapshot = asyncio.run(exercise())

    assert observed == {0: [10], 1: [20]}
    assert snapshot.state == ViewportDispatchState.CLOSED
    assert snapshot.viewport_count == 2
    assert snapshot.delivered_frames == 2
    assert snapshot.delivered_frame_bytes == 8
    assert snapshot.max_source_span_ms == 20
    payload = snapshot.model_dump_json()
    assert "rtsp://" not in payload
    assert "recording_id" not in payload
    assert "source_id" not in payload
    assert "abcd" not in payload


def test_unknown_duplicate_and_out_of_range_slots_fail_closed_or_validate() -> None:
    async def consume(_frame: PresentationVideoFrame) -> None:
        return None

    dispatcher = BoundedViewportDispatcher([ViewportBinding(1, consume)])
    with pytest.raises(ViewportDispatchError) as unknown_exc:
        asyncio.run(dispatcher.dispatch(2, _frame(0)))
    assert unknown_exc.value.code == ViewportDispatchErrorCode.UNKNOWN_SLOT
    assert dispatcher.snapshot.state == ViewportDispatchState.OPEN

    with pytest.raises(ViewportDispatchError) as duplicate_exc:
        BoundedViewportDispatcher([ViewportBinding(1, consume), ViewportBinding(1, consume)])
    assert duplicate_exc.value.code == ViewportDispatchErrorCode.INVALID_BINDINGS

    with pytest.raises(ValueError):
        ViewportBinding(64, consume)


def test_consumer_failure_and_timeout_are_sanitized_and_release_bindings() -> None:
    async def fail(_frame: PresentationVideoFrame) -> None:
        raise RuntimeError("SECRET renderer detail")

    failed = BoundedViewportDispatcher([ViewportBinding(0, fail)])
    with pytest.raises(ViewportDispatchError) as failure_exc:
        asyncio.run(failed.dispatch(0, _frame(0)))
    assert failure_exc.value.code == ViewportDispatchErrorCode.CONSUMER_FAILURE
    assert "SECRET" not in str(failure_exc.value)
    assert failed.snapshot.state == ViewportDispatchState.FAILED
    with pytest.raises(ViewportDispatchError) as reuse_exc:
        asyncio.run(failed.dispatch(0, _frame(1)))
    assert reuse_exc.value.code == ViewportDispatchErrorCode.INVALID_STATE

    async def slow(_frame: PresentationVideoFrame) -> None:
        await asyncio.sleep(0.05)

    timed = BoundedViewportDispatcher(
        [ViewportBinding(0, slow)],
        consumer_timeout_seconds=0.001,
    )
    with pytest.raises(ViewportDispatchError) as timeout_exc:
        asyncio.run(timed.dispatch(0, _frame(0)))
    assert timeout_exc.value.code == ViewportDispatchErrorCode.CONSUMER_TIMEOUT
    assert timed.snapshot.state == ViewportDispatchState.FAILED


def test_frame_and_byte_limits_fail_closed() -> None:
    async def consume(_frame: PresentationVideoFrame) -> None:
        return None

    frame_limited = BoundedViewportDispatcher(
        [ViewportBinding(0, consume)],
        max_total_frames=1,
    )

    async def hit_frame_limit() -> None:
        await frame_limited.dispatch(0, _frame(0))
        await frame_limited.dispatch(0, _frame(1))

    with pytest.raises(ViewportDispatchError) as frame_exc:
        asyncio.run(hit_frame_limit())
    assert frame_exc.value.code == ViewportDispatchErrorCode.FRAME_LIMIT
    assert frame_limited.snapshot.state == ViewportDispatchState.FAILED

    per_frame_limited = BoundedViewportDispatcher(
        [ViewportBinding(0, consume)],
        max_frame_bytes=3,
    )
    with pytest.raises(ViewportDispatchError) as per_frame_exc:
        asyncio.run(per_frame_limited.dispatch(0, _frame(0)))
    assert per_frame_exc.value.code == ViewportDispatchErrorCode.FRAME_BYTES_LIMIT

    total_limited = BoundedViewportDispatcher(
        [ViewportBinding(0, consume)],
        max_total_frame_bytes=4,
    )

    async def hit_total_limit() -> None:
        await total_limited.dispatch(0, _frame(0))
        await total_limited.dispatch(0, _frame(1))

    with pytest.raises(ViewportDispatchError) as total_exc:
        asyncio.run(hit_total_limit())
    assert total_exc.value.code == ViewportDispatchErrorCode.TOTAL_BYTES_LIMIT


def test_close_and_constructor_bounds() -> None:
    async def consume(_frame: PresentationVideoFrame) -> None:
        return None

    dispatcher = BoundedViewportDispatcher([ViewportBinding(0, consume)])
    snapshot = asyncio.run(dispatcher.close())
    assert snapshot.state == ViewportDispatchState.CLOSED
    assert asyncio.run(dispatcher.close()).state == ViewportDispatchState.CLOSED
    with pytest.raises(ViewportDispatchError) as exc:
        asyncio.run(dispatcher.dispatch(0, _frame(0)))
    assert exc.value.code == ViewportDispatchErrorCode.INVALID_STATE

    with pytest.raises(ValueError):
        BoundedViewportDispatcher([ViewportBinding(0, consume)], max_viewports=0)
    with pytest.raises(ValueError):
        BoundedViewportDispatcher([ViewportBinding(0, consume)], max_total_frames=0)
    with pytest.raises(ValueError):
        BoundedViewportDispatcher([ViewportBinding(0, consume)], max_frame_bytes=0)
    with pytest.raises(ValueError):
        BoundedViewportDispatcher([ViewportBinding(0, consume)], max_total_frame_bytes=0)
