from __future__ import annotations

import asyncio

import pytest

from k5vision.media.gstreamer_playback_decoder import (
    NativePlaybackDecoderError,
    NativePlaybackDecoderErrorCode,
    NativePlaybackDecoderState,
)
from k5vision.media.presentation_decoder import (
    GStreamerPresentationDecoder,
    _PresentationPayload,
)
from k5vision.media.presentation_frame import PixelFormat


def _rtp(payload: bytes = b"x") -> memoryview:
    return memoryview(
        bytes(
            [
                0x80,
                96,
                0,
                1,
                0,
                1,
                226,
                64,
                0,
                0,
                0,
                1,
            ]
        )
        + payload
    )


class FakePresentationBackend:
    def __init__(self) -> None:
        self.frames: list[object] = []
        self.flush_frames: list[object] = []
        self.closed = False
        self.fail_push = False

    def push(self, _packet: bytes) -> list[object]:
        if self.fail_push:
            raise RuntimeError("rtsp://user:secret@192.0.2.90/private")
        return list(self.frames)

    def end_of_stream(self) -> list[object]:
        return list(self.flush_frames)

    def close(self) -> None:
        self.closed = True


def _decoder(backend: FakePresentationBackend, **kwargs: object) -> GStreamerPresentationDecoder:
    return GStreamerPresentationDecoder(96, backend=backend, **kwargs)  # type: ignore[arg-type]


def _payload(*, width: int = 2, height: int = 2, stride: int = 8) -> _PresentationPayload:
    return _PresentationPayload(
        payload=bytes(stride * height),
        width=width,
        height=height,
        stride_bytes=stride,
    )


def test_decode_emits_validated_presentation_metadata() -> None:
    backend = FakePresentationBackend()
    backend.frames = [_payload(width=4, height=2, stride=16)]
    decoder = _decoder(backend)

    frames = asyncio.run(decoder.decode(_rtp(), 250))

    assert len(frames) == 1
    frame = frames[0]
    assert frame.width == 4
    assert frame.height == 2
    assert frame.stride_bytes == 16
    assert frame.pixel_format == PixelFormat.BGRX
    assert frame.source_elapsed_ms == 250
    assert len(frame.payload) == 32
    assert decoder.snapshot.state == NativePlaybackDecoderState.RUNNING
    assert decoder.snapshot.emitted_frames == 1
    assert decoder.snapshot.emitted_bytes == 32
    serialized = str(decoder.snapshot.model_dump())
    assert "192.0.2" not in serialized
    assert "payload" not in serialized


def test_flush_emits_presentation_frame_and_close_is_idempotent() -> None:
    backend = FakePresentationBackend()
    backend.flush_frames = [_payload()]
    decoder = _decoder(backend)

    frames = asyncio.run(decoder.flush())
    asyncio.run(decoder.close())
    asyncio.run(decoder.close())

    assert len(frames) == 1
    assert frames[0].pixel_format == PixelFormat.BGRX
    assert backend.closed is True
    assert decoder.snapshot.state == NativePlaybackDecoderState.CLOSED


def test_invalid_geometry_from_backend_fails_closed() -> None:
    backend = FakePresentationBackend()
    backend.frames = [_payload(width=3, height=2, stride=8)]
    decoder = _decoder(backend)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert decoder.snapshot.state == NativePlaybackDecoderState.FAILED


def test_unexpected_backend_frame_type_fails_closed() -> None:
    backend = FakePresentationBackend()
    backend.frames = [b"not-metadata"]
    decoder = _decoder(backend)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert decoder.snapshot.state == NativePlaybackDecoderState.FAILED


def test_backend_failure_is_sanitized() -> None:
    backend = FakePresentationBackend()
    backend.fail_push = True
    decoder = _decoder(backend)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert "secret" not in str(caught.value)
    assert "192.0.2.90" not in str(caught.value)


def test_frame_count_bound_is_inherited() -> None:
    backend = FakePresentationBackend()
    backend.frames = [_payload(), _payload()]
    decoder = _decoder(backend, max_frames_per_push=1)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.FRAME_LIMIT


def test_frame_byte_bound_is_inherited() -> None:
    backend = FakePresentationBackend()
    backend.frames = [_payload()]
    decoder = _decoder(backend, max_frame_bytes=8)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.FRAME_TOO_LARGE
