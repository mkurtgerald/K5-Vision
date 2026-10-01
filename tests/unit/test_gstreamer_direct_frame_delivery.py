from __future__ import annotations

import asyncio
import ctypes
from unittest.mock import Mock

import pytest

from k5vision.media.gstreamer_direct_frame_delivery import (
    GStreamerDirectFrameDelivery,
    _DirectRtspFrameBackend,
)
from k5vision.media.live_presentation import LivePresentationError, LivePresentationErrorCode
from k5vision.media.presentation_decoder import _PresentationPayload
from k5vision.media.presentation_frame import PixelFormat


@pytest.mark.parametrize("timeout_ms", [500, 5_000, 30_000])
def test_direct_backend_preserves_64_bit_native_startup_timeout(timeout_ms: int) -> None:
    observed: list[int] = []
    native_signature = ctypes.CFUNCTYPE(
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint64,
    )

    @native_signature
    def native_get_state(_pipeline, _current, _pending, timeout_ns):
        observed.append(timeout_ns)
        return 2

    # Model a freshly loaded native symbol: its argument types are undeclared
    # until the backend binds them. A plain Mock cannot expose C int truncation.
    untyped_get_state = ctypes.CFUNCTYPE(ctypes.c_int)(
        ctypes.cast(native_get_state, ctypes.c_void_p).value
    )
    backend = _DirectRtspFrameBackend.__new__(_DirectRtspFrameBackend)
    backend._core = Mock()
    backend._app = Mock()
    backend._core.gst_element_get_state = untyped_get_state
    backend._bind_signatures()

    current = ctypes.c_int()
    pending = ctypes.c_int()
    result = backend._core.gst_element_get_state(
        ctypes.c_void_p(),
        ctypes.byref(current),
        ctypes.byref(pending),
        timeout_ms * 1_000_000,
    )

    assert result == 2
    assert observed == [timeout_ms * 1_000_000]


def _payload(byte_value: int) -> _PresentationPayload:
    return _PresentationPayload(
        payload=bytes([byte_value]) * 16,
        width=2,
        height=2,
        stride_bytes=8,
    )


class _Backend:
    def __init__(self, frames: list[_PresentationPayload | None]) -> None:
        self._frames = list(frames)
        self.closed = False

    def pull(self, _timeout_ms: int) -> _PresentationPayload | None:
        if self._frames:
            return self._frames.pop(0)
        return None

    def close(self) -> None:
        self.closed = True


def test_direct_frame_delivery_presents_bounded_decoded_frames() -> None:
    backend = _Backend([_payload(1), None, _payload(2)])
    observed: list[tuple[int, int, int, PixelFormat, int]] = []

    async def consume(frame) -> None:  # type: ignore[no-untyped-def]
        observed.append(
            (
                frame.width,
                frame.height,
                frame.stride_bytes,
                frame.pixel_format,
                len(frame.payload),
            )
        )

    delivery = GStreamerDirectFrameDelivery(
        frame_goal=2,
        delivery_timeout_seconds=1.0,
        consumer_timeout_seconds=0.5,
        backend_factory=lambda _uri, _max, _probe: backend,
    )

    snapshot = asyncio.run(delivery.run("rtsp://127.0.0.1:8554/k5synthetic", consume))

    assert snapshot.state.value == "complete"
    assert snapshot.decoder_initialized is True
    assert snapshot.delivered_frames == 2
    assert snapshot.delivered_frame_bytes == 32
    assert snapshot.rtp_valid_packets == 0
    assert snapshot.rtp_delivered_bytes == 0
    assert observed == [
        (2, 2, 8, PixelFormat.BGRX, 16),
        (2, 2, 8, PixelFormat.BGRX, 16),
    ]
    assert backend.closed is True


def test_direct_frame_delivery_sanitizes_backend_start_failure() -> None:
    source = "rtsp://user:secret@127.0.0.1:8554/k5synthetic"

    def failing_factory(_uri: str, _max: int, _probe: int) -> _Backend:
        raise RuntimeError(f"failed {source}")

    delivery = GStreamerDirectFrameDelivery(
        frame_goal=1,
        delivery_timeout_seconds=1.0,
        backend_factory=failing_factory,
    )

    async def scenario() -> None:
        with pytest.raises(LivePresentationError) as caught:
            await delivery.run(source, lambda _frame: asyncio.sleep(0))
        assert caught.value.code == LivePresentationErrorCode.DECODER_INIT_FAILURE
        detail = str(caught.value)
        assert "rtsp://" not in detail
        assert "user" not in detail
        assert "secret" not in detail

    asyncio.run(scenario())


def test_direct_frame_delivery_closes_backend_on_consumer_failure() -> None:
    backend = _Backend([_payload(3)])
    delivery = GStreamerDirectFrameDelivery(
        frame_goal=1,
        delivery_timeout_seconds=1.0,
        backend_factory=lambda _uri, _max, _probe: backend,
    )

    async def failing_consumer(_frame) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic consumer failure")

    async def scenario() -> None:
        with pytest.raises(LivePresentationError) as caught:
            await delivery.run("rtsp://127.0.0.1:8554/k5synthetic", failing_consumer)
        assert caught.value.code == LivePresentationErrorCode.CONSUMER_FAILURE

    asyncio.run(scenario())
    assert backend.closed is True
