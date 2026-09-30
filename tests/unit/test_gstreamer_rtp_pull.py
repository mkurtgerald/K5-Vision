from __future__ import annotations

import asyncio

import pytest

from k5vision.media.gstreamer_rtp_pull import GStreamerDirectRtpDelivery
from k5vision.media.rtp_delivery import RtpDeliveryError, RtpDeliveryErrorCode


def _rtp_packet(payload_type: int = 96, sequence: int = 1) -> bytes:
    header = bytes(
        [
            0x80,
            payload_type & 0x7F,
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
            0,
            0,
            0,
            sequence & 0xFF,
            0,
            0,
            0,
            1,
        ]
    )
    return header + b"payload"


class _Backend:
    def __init__(self, packets: list[bytes | None]) -> None:
        self._packets = list(packets)
        self.closed = False

    def pull(self, _timeout_ms: int) -> bytes | None:
        if self._packets:
            return self._packets.pop(0)
        return None

    def close(self) -> None:
        self.closed = True


def test_direct_rtp_delivery_feeds_valid_packets_without_udp_socket() -> None:
    backend = _Backend([_rtp_packet(sequence=1), _rtp_packet(sequence=2)])
    received: list[bytes] = []

    async def consume(packet: memoryview) -> None:
        received.append(bytes(packet))

    delivery = GStreamerDirectRtpDelivery(
        packet_goal=2,
        delivery_timeout_seconds=1.0,
        consumer_timeout_seconds=0.5,
        backend_factory=lambda _uri, _max, _probe: backend,
    )

    result = asyncio.run(delivery.deliver("rtsp://127.0.0.1:8554/k5synthetic", consume))

    assert result.valid_packets == 2
    assert result.invalid_packets == 0
    assert result.delivered_bytes == sum(len(item) for item in received)
    assert received == [_rtp_packet(sequence=1), _rtp_packet(sequence=2)]
    assert backend.closed is True


def test_direct_rtp_delivery_ignores_invalid_packet_then_delivers_valid() -> None:
    backend = _Backend([b"not-rtp", _rtp_packet()])
    received = 0

    async def consume(_packet: memoryview) -> None:
        nonlocal received
        received += 1

    delivery = GStreamerDirectRtpDelivery(
        packet_goal=1,
        delivery_timeout_seconds=1.0,
        backend_factory=lambda _uri, _max, _probe: backend,
    )

    result = asyncio.run(delivery.deliver("rtsp://127.0.0.1:8554/k5synthetic", consume))

    assert result.valid_packets == 1
    assert result.invalid_packets == 1
    assert received == 1
    assert backend.closed is True


def test_direct_rtp_delivery_sanitizes_backend_start_failure() -> None:
    source = "rtsp://user:secret@127.0.0.1:8554/k5synthetic"

    def failing_factory(_uri: str, _max: int, _probe: int) -> _Backend:
        raise RuntimeError(f"failed {source}")

    delivery = GStreamerDirectRtpDelivery(
        packet_goal=1,
        delivery_timeout_seconds=1.0,
        backend_factory=failing_factory,
    )

    async def scenario() -> None:
        with pytest.raises(RtpDeliveryError) as caught:
            await delivery.deliver(source, lambda _packet: asyncio.sleep(0))
        assert caught.value.code == RtpDeliveryErrorCode.RUNTIME_FAILURE
        detail = str(caught.value)
        assert "rtsp://" not in detail
        assert "user" not in detail
        assert "secret" not in detail

    asyncio.run(scenario())
