from __future__ import annotations

import asyncio
import time

import pytest

from k5vision.media.gstreamer_playback_decoder import (
    GStreamerNativePlaybackDecoder,
    NativePlaybackDecoderError,
    NativePlaybackDecoderErrorCode,
    NativePlaybackDecoderState,
)


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


class FakeBackend:
    def __init__(self) -> None:
        self.pushed: list[bytes] = []
        self.frames: list[bytes] = []
        self.flush_frames: list[bytes] = []
        self.closed = False
        self.fail_push = False
        self.fail_flush = False
        self.fail_close = False
        self.block_push = False

    def push(self, packet: bytes) -> list[bytes]:
        if self.block_push:
            time.sleep(0.1)
        if self.fail_push:
            raise RuntimeError("rtsp://user:secret@192.0.2.44/private")
        self.pushed.append(packet)
        return list(self.frames)

    def end_of_stream(self) -> list[bytes]:
        if self.fail_flush:
            raise RuntimeError("C:/private/runtime/detail")
        return list(self.flush_frames)

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("private cleanup detail")


def _decoder(backend: FakeBackend, **kwargs: object) -> GStreamerNativePlaybackDecoder:
    return GStreamerNativePlaybackDecoder(96, backend=backend, **kwargs)


def test_decode_emits_transient_frames_and_source_free_snapshot() -> None:
    backend = FakeBackend()
    backend.frames = [b"frame-a", b"frame-b"]
    decoder = _decoder(backend)

    frames = asyncio.run(decoder.decode(_rtp(b"payload"), 125))

    assert [bytes(frame.payload) for frame in frames] == [b"frame-a", b"frame-b"]
    assert [frame.source_elapsed_ms for frame in frames] == [125, 125]
    assert decoder.snapshot.state == NativePlaybackDecoderState.RUNNING
    assert decoder.snapshot.pushed_packets == 1
    assert decoder.snapshot.emitted_frames == 2
    assert decoder.snapshot.emitted_bytes == 14
    serialized = str(decoder.snapshot.model_dump())
    assert "payload" not in serialized
    assert "frame-a" not in serialized


def test_flush_is_idempotent_and_close_is_idempotent() -> None:
    backend = FakeBackend()
    backend.flush_frames = [b"tail"]
    decoder = _decoder(backend)

    first = asyncio.run(decoder.flush())
    second = asyncio.run(decoder.flush())
    asyncio.run(decoder.close())
    asyncio.run(decoder.close())

    assert [bytes(frame.payload) for frame in first] == [b"tail"]
    assert second == []
    assert backend.closed is True
    assert decoder.snapshot.state == NativePlaybackDecoderState.CLOSED


def test_invalid_rtp_fails_before_backend() -> None:
    backend = FakeBackend()
    decoder = _decoder(backend)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(memoryview(b"not-rtp"), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.INVALID_PACKET
    assert backend.pushed == []


def test_invalid_timing_fails_before_backend() -> None:
    backend = FakeBackend()
    decoder = _decoder(backend)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), -1))

    assert caught.value.code == NativePlaybackDecoderErrorCode.INVALID_PACKET
    assert backend.pushed == []


def test_backend_failure_is_sanitized() -> None:
    backend = FakeBackend()
    backend.fail_push = True
    decoder = _decoder(backend)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert "secret" not in str(caught.value)
    assert "192.0.2.44" not in str(caught.value)
    assert decoder.snapshot.state == NativePlaybackDecoderState.FAILED


def test_backend_timeout_is_bounded() -> None:
    backend = FakeBackend()
    backend.block_push = True
    decoder = _decoder(backend, operation_timeout_seconds=0.01)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert decoder.snapshot.state == NativePlaybackDecoderState.FAILED


def test_frame_count_limit_fails_closed() -> None:
    backend = FakeBackend()
    backend.frames = [b"a", b"b"]
    decoder = _decoder(backend, max_frames_per_push=1)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.FRAME_LIMIT
    assert decoder.snapshot.state == NativePlaybackDecoderState.FAILED


def test_frame_byte_limit_fails_closed() -> None:
    backend = FakeBackend()
    backend.frames = [b"ab"]
    decoder = _decoder(backend, max_frame_bytes=1)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.FRAME_TOO_LARGE
    assert decoder.snapshot.state == NativePlaybackDecoderState.FAILED


def test_flush_failure_is_sanitized() -> None:
    backend = FakeBackend()
    backend.fail_flush = True
    decoder = _decoder(backend)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.flush())

    assert caught.value.code == NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert "private" not in str(caught.value)
    assert decoder.snapshot.state == NativePlaybackDecoderState.FAILED


def test_cleanup_failure_is_sanitized() -> None:
    backend = FakeBackend()
    backend.fail_close = True
    decoder = _decoder(backend)

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.close())

    assert caught.value.code == NativePlaybackDecoderErrorCode.NATIVE_FAILURE
    assert "private" not in str(caught.value)
    assert decoder.snapshot.state == NativePlaybackDecoderState.FAILED


def test_decode_rejected_after_eos() -> None:
    backend = FakeBackend()
    decoder = _decoder(backend)
    asyncio.run(decoder.flush())

    with pytest.raises(NativePlaybackDecoderError) as caught:
        asyncio.run(decoder.decode(_rtp(), 0))

    assert caught.value.code == NativePlaybackDecoderErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_packet_bytes": 11}, "max_packet_bytes"),
        ({"max_frame_bytes": 0}, "max_frame_bytes"),
        ({"max_frames_per_push": 0}, "max_frames_per_push"),
        ({"operation_timeout_seconds": 0}, "operation_timeout_seconds"),
        ({"pull_timeout_ms": 101}, "pull_timeout_ms"),
    ],
)
def test_constructor_bounds_fail_closed(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        GStreamerNativePlaybackDecoder(96, backend=FakeBackend(), **kwargs)


def test_dynamic_h264_payload_type_required() -> None:
    with pytest.raises(ValueError, match="payload_type"):
        GStreamerNativePlaybackDecoder(26, backend=FakeBackend())
