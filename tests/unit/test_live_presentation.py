from __future__ import annotations

import asyncio

import pytest

from k5vision.media.live_presentation import (
    BoundedLivePresentationDelivery,
    LivePresentationError,
    LivePresentationErrorCode,
    LivePresentationState,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.rtp_delivery import RtpDeliveryError, RtpDeliveryErrorCode, RtpDeliveryResult


def _rtp(*, sequence: int, timestamp: int, payload_type: int = 96) -> bytes:
    return bytes(
        [
            0x80,
            payload_type,
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
            (timestamp >> 24) & 0xFF,
            (timestamp >> 16) & 0xFF,
            (timestamp >> 8) & 0xFF,
            timestamp & 0xFF,
            0,
            0,
            0,
            1,
            0x65,
        ]
    )


class FakeRtpDelivery:
    def __init__(
        self,
        packets: list[bytes],
        *,
        invalid_packets: int = 0,
        fail_code: RtpDeliveryErrorCode | None = None,
    ) -> None:
        self.packets = packets
        self.invalid_packets = invalid_packets
        self.fail_code = fail_code
        self.seen_source: str | None = None

    async def deliver(self, source_uri: str, consumer: object) -> RtpDeliveryResult:
        self.seen_source = source_uri
        if self.fail_code is not None:
            raise RtpDeliveryError(self.fail_code, "SECRET transport detail")
        delivered_bytes = 0
        for packet in self.packets:
            try:
                await consumer(memoryview(packet))  # type: ignore[operator]
            except asyncio.CancelledError:
                raise
            except Exception:
                raise RtpDeliveryError(
                    RtpDeliveryErrorCode.CONSUMER_FAILURE,
                    "RTP consumer failed",
                ) from None
            delivered_bytes += len(packet)
        return RtpDeliveryResult(
            valid_packets=len(self.packets),
            invalid_packets=self.invalid_packets,
            delivered_bytes=delivered_bytes,
            elapsed_ms=10,
        )


class FakeDecoder:
    def __init__(
        self,
        *,
        fail: bool = False,
        close_fail: bool = False,
        flush_frame: bool = False,
    ) -> None:
        self.fail = fail
        self.close_fail = close_fail
        self.flush_frame = flush_frame
        self.closed = False

    @staticmethod
    def _frame(source_elapsed_ms: int) -> PresentationVideoFrame:
        return PresentationVideoFrame(
            payload=memoryview(b"abcd"),
            width=1,
            height=1,
            stride_bytes=4,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=source_elapsed_ms,
        )

    async def decode(
        self,
        _packet: memoryview,
        source_elapsed_ms: int,
    ) -> list[PresentationVideoFrame]:
        if self.fail:
            raise RuntimeError("SECRET decoder detail")
        return [self._frame(source_elapsed_ms)]

    async def flush(self) -> list[PresentationVideoFrame]:
        if self.flush_frame:
            return [self._frame(0)]
        return []

    async def close(self) -> None:
        self.closed = True
        if self.close_fail:
            raise RuntimeError("SECRET cleanup detail")


def _delivery(
    packets: list[bytes],
    decoder: FakeDecoder,
    **kwargs: object,
) -> BoundedLivePresentationDelivery:
    return BoundedLivePresentationDelivery(
        96,
        decoder_factory=lambda _payload_type: decoder,
        rtp_delivery=FakeRtpDelivery(packets),
        **kwargs,
    )


def test_live_delivery_normalizes_wrapped_rtp_timing_and_is_source_free() -> None:
    origin = 0xFFFFFFF0
    later = (origin + 9000) & 0xFFFFFFFF
    packets = [_rtp(sequence=1, timestamp=origin), _rtp(sequence=2, timestamp=later)]
    decoder = FakeDecoder(flush_frame=True)
    delivery = _delivery(packets, decoder)
    source = "rtsp://user:SECRET@192.168.1.50/private"
    observed: list[tuple[bytes, int]] = []

    async def consume(frame: PresentationVideoFrame) -> None:
        observed.append((bytes(frame.payload), frame.source_elapsed_ms))

    snapshot = asyncio.run(delivery.run(source, consume))

    assert observed == [(b"abcd", 0), (b"abcd", 100), (b"abcd", 100)]
    assert snapshot.state == LivePresentationState.COMPLETE
    assert snapshot.decoder_initialized is True
    assert snapshot.accepted_packets == 2
    assert snapshot.rtp_valid_packets == 2
    assert snapshot.delivered_frames == 3
    assert snapshot.delivered_frame_bytes == 12
    assert snapshot.source_span_ms == 100
    assert decoder.closed is True
    payload = snapshot.model_dump_json()
    assert source not in payload
    assert "192.168" not in payload
    assert "SECRET" not in payload
    assert "abcd" not in payload


def test_payload_type_mismatch_fails_closed_and_closes_decoder() -> None:
    decoder = FakeDecoder()
    delivery = _delivery([_rtp(sequence=1, timestamp=1, payload_type=97)], decoder)

    async def consume(_frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(LivePresentationError) as exc:
        asyncio.run(delivery.run("rtsp://SECRET-source", consume))

    assert exc.value.code == LivePresentationErrorCode.INVALID_RTP
    assert "SECRET" not in str(exc.value)
    assert delivery.snapshot.state == LivePresentationState.FAILED
    assert decoder.closed is True


def test_decoder_failure_is_sanitized() -> None:
    decoder = FakeDecoder(fail=True)
    delivery = _delivery([_rtp(sequence=1, timestamp=1)], decoder)

    async def consume(_frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(LivePresentationError) as exc:
        asyncio.run(delivery.run("rtsp://SECRET-source", consume))

    assert exc.value.code == LivePresentationErrorCode.DECODER_FAILURE
    assert "SECRET" not in str(exc.value)
    assert delivery.snapshot.state == LivePresentationState.FAILED
    assert decoder.closed is True


def test_consumer_failure_is_sanitized() -> None:
    decoder = FakeDecoder()
    delivery = _delivery([_rtp(sequence=1, timestamp=1)], decoder)

    async def consume(_frame: PresentationVideoFrame) -> None:
        raise RuntimeError("SECRET consumer detail")

    with pytest.raises(LivePresentationError) as exc:
        asyncio.run(delivery.run("rtsp://SECRET-source", consume))

    assert exc.value.code == LivePresentationErrorCode.CONSUMER_FAILURE
    assert "SECRET" not in str(exc.value)
    assert delivery.snapshot.state == LivePresentationState.FAILED


def test_delivery_timeout_is_normalized() -> None:
    decoder = FakeDecoder()
    delivery = BoundedLivePresentationDelivery(
        96,
        decoder_factory=lambda _payload_type: decoder,
        rtp_delivery=FakeRtpDelivery([], fail_code=RtpDeliveryErrorCode.TIMEOUT),
    )

    async def consume(_frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(LivePresentationError) as exc:
        asyncio.run(delivery.run("rtsp://SECRET-source", consume))

    assert exc.value.code == LivePresentationErrorCode.DELIVERY_TIMEOUT
    assert "SECRET" not in str(exc.value)
    assert decoder.closed is True


def test_cleanup_failure_fails_closed_after_successful_delivery() -> None:
    decoder = FakeDecoder(close_fail=True)
    delivery = _delivery([_rtp(sequence=1, timestamp=1)], decoder)

    async def consume(_frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(LivePresentationError) as exc:
        asyncio.run(delivery.run("rtsp://source", consume))

    assert exc.value.code == LivePresentationErrorCode.CLEANUP_FAILURE
    assert delivery.snapshot.state == LivePresentationState.FAILED


def test_single_use_and_constructor_bounds_fail_closed() -> None:
    decoder = FakeDecoder()
    delivery = _delivery([_rtp(sequence=1, timestamp=1)], decoder)

    async def consume(_frame: PresentationVideoFrame) -> None:
        return None

    asyncio.run(delivery.run("rtsp://source", consume))
    with pytest.raises(LivePresentationError) as exc:
        asyncio.run(delivery.run("rtsp://source", consume))
    assert exc.value.code == LivePresentationErrorCode.INVALID_STATE

    with pytest.raises(ValueError):
        BoundedLivePresentationDelivery(95)
    with pytest.raises(ValueError):
        BoundedLivePresentationDelivery(96, clock_rate_hz=8000)
    with pytest.raises(ValueError):
        BoundedLivePresentationDelivery(96, max_frames=0)
    with pytest.raises(ValueError):
        BoundedLivePresentationDelivery(96, max_source_span_ms=0)
    with pytest.raises(TypeError):
        BoundedLivePresentationDelivery(96, decoder_factory=0)  # type: ignore[arg-type]
