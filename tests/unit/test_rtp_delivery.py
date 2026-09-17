from __future__ import annotations

import asyncio
import socket

import pytest

from k5vision.media.rtp_delivery import (
    EphemeralRtpDelivery,
    RtpDeliveryError,
    RtpDeliveryErrorCode,
    is_rtp_v2,
)


def _rtp_packet(sequence: int, payload: bytes = b"k5") -> bytes:
    return (
        bytes((0x80, 96))
        + sequence.to_bytes(2, "big")
        + (1234 + sequence).to_bytes(4, "big")
        + (5678).to_bytes(4, "big")
        + payload
    )


class FakeRelay:
    def __init__(
        self,
        port: int,
        packets: list[bytes],
        *,
        fail_start: bool = False,
    ) -> None:
        self.port = port
        self.packets = packets
        self.fail_start = fail_start
        self.closed = False

    async def start(self, source_uri: str) -> None:
        if self.fail_start:
            raise RuntimeError(f"unsafe source: {source_uri}")
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for packet in self.packets:
                sender.sendto(packet, ("127.0.0.1", self.port))
        finally:
            sender.close()

    async def close(self) -> None:
        self.closed = True


def test_rtp_header_validation_is_bounded() -> None:
    assert is_rtp_v2(_rtp_packet(1))
    assert not is_rtp_v2(b"short")
    assert not is_rtp_v2(bytes((0x40, 96)) + b"x" * 20)

    extension_header = bytes((0x90, 96)) + b"\x00" * 10
    assert not is_rtp_v2(extension_header)


def test_delivery_invokes_consumer_and_retains_only_counters() -> None:
    async def exercise() -> None:
        relays: list[FakeRelay] = []

        def relay_factory(port: int) -> FakeRelay:
            relay = FakeRelay(port, [b"bad", _rtp_packet(1), _rtp_packet(2)])
            relays.append(relay)
            return relay

        seen_lengths: list[int] = []

        async def consumer(packet: memoryview) -> None:
            seen_lengths.append(len(packet))

        delivery = EphemeralRtpDelivery(
            packet_goal=2,
            delivery_timeout_seconds=1.0,
            relay_factory=relay_factory,
        )
        result = await delivery.deliver("rtsp://user:secret@192.0.2.20/live", consumer)

        assert result.valid_packets == 2
        assert result.invalid_packets == 1
        assert result.delivered_bytes == sum(seen_lengths)
        assert len(seen_lengths) == 2
        assert relays[0].closed
        payload = result.model_dump_json()
        assert "rtsp" not in payload
        assert "192.0.2.20" not in payload
        assert "secret" not in payload

    asyncio.run(exercise())


def test_runtime_failure_is_sanitized_and_cleanup_runs() -> None:
    async def exercise() -> None:
        relays: list[FakeRelay] = []

        def relay_factory(port: int) -> FakeRelay:
            relay = FakeRelay(port, [], fail_start=True)
            relays.append(relay)
            return relay

        async def consumer(packet: memoryview) -> None:
            raise AssertionError("consumer must not run")

        source = "rtsp://username:password@192.0.2.44/private"
        delivery = EphemeralRtpDelivery(relay_factory=relay_factory)

        with pytest.raises(RtpDeliveryError) as caught:
            await delivery.deliver(source, consumer)

        assert caught.value.code == RtpDeliveryErrorCode.RUNTIME_FAILURE
        rendered = str(caught.value)
        assert source not in rendered
        assert "username" not in rendered
        assert "password" not in rendered
        assert "192.0.2.44" not in rendered
        assert relays[0].closed

    asyncio.run(exercise())


def test_delivery_timeout_is_bounded_and_cleanup_runs() -> None:
    async def exercise() -> None:
        relays: list[FakeRelay] = []

        def relay_factory(port: int) -> FakeRelay:
            relay = FakeRelay(port, [])
            relays.append(relay)
            return relay

        async def consumer(packet: memoryview) -> None:
            raise AssertionError("consumer must not run")

        delivery = EphemeralRtpDelivery(
            packet_goal=1,
            delivery_timeout_seconds=0.01,
            relay_factory=relay_factory,
        )

        with pytest.raises(RtpDeliveryError) as caught:
            await delivery.deliver("rtsp://example.invalid/live", consumer)

        assert caught.value.code == RtpDeliveryErrorCode.TIMEOUT
        assert relays[0].closed

    asyncio.run(exercise())


def test_slow_consumer_fails_closed() -> None:
    async def exercise() -> None:
        relays: list[FakeRelay] = []

        def relay_factory(port: int) -> FakeRelay:
            relay = FakeRelay(port, [_rtp_packet(1)])
            relays.append(relay)
            return relay

        async def slow_consumer(packet: memoryview) -> None:
            await asyncio.sleep(0.1)

        delivery = EphemeralRtpDelivery(
            packet_goal=1,
            delivery_timeout_seconds=1.0,
            consumer_timeout_seconds=0.01,
            relay_factory=relay_factory,
        )

        with pytest.raises(RtpDeliveryError) as caught:
            await delivery.deliver("rtsp://example.invalid/live", slow_consumer)

        assert caught.value.code == RtpDeliveryErrorCode.CONSUMER_FAILURE
        assert relays[0].closed

    asyncio.run(exercise())
