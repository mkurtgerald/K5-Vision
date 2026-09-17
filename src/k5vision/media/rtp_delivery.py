"""Ephemeral RTP delivery into a K5-owned consumer boundary.

Media datagrams exist only long enough to validate an RTP header and invoke the
consumer callback. This module retains only source-free counters after delivery.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.gstreamer_rtp_relay import GStreamerRtpRelayRuntime

RtpConsumer = Callable[[memoryview], Awaitable[None]]


class RtpRelay(Protocol):
    async def start(self, source_uri: str) -> None: ...

    async def close(self) -> None: ...


RtpRelayFactory = Callable[[int], RtpRelay]


class RtpDeliveryErrorCode(StrEnum):
    TIMEOUT = "timeout"
    RUNTIME_FAILURE = "runtime_failure"
    CONSUMER_FAILURE = "consumer_failure"


class RtpDeliveryError(RuntimeError):
    """Source-free RTP delivery failure."""

    def __init__(self, code: RtpDeliveryErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class RtpDeliveryResult(BaseModel):
    """Non-media observability returned after bounded delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    valid_packets: int = Field(ge=1, le=4096)
    invalid_packets: int = Field(ge=0, le=4096)
    delivered_bytes: int = Field(ge=1)
    elapsed_ms: int = Field(ge=0, le=600_000)


def is_rtp_v2(packet: bytes | memoryview) -> bool:
    """Validate the bounded RTP v2 fixed/header-extension structure."""
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return False
    csrc_count = packet[0] & 0x0F
    header_length = 12 + (csrc_count * 4)
    if len(packet) < header_length:
        return False
    has_extension = bool(packet[0] & 0x10)
    if not has_extension:
        return True
    if len(packet) < header_length + 4:
        return False
    extension_words = int.from_bytes(packet[header_length + 2 : header_length + 4], "big")
    return len(packet) >= header_length + 4 + (extension_words * 4)


class EphemeralRtpDelivery:
    """Deliver bounded video RTP packets to one callback without retaining payloads."""

    def __init__(
        self,
        *,
        packet_goal: int = 16,
        delivery_timeout_seconds: float = 10.0,
        consumer_timeout_seconds: float = 0.5,
        max_datagram_bytes: int = 65_535,
        receive_buffer_bytes: int = 262_144,
        relay_startup_probe_seconds: float = 0.15,
        relay_factory: RtpRelayFactory | None = None,
    ) -> None:
        if not 1 <= packet_goal <= 4096:
            raise ValueError("packet_goal must be between 1 and 4096")
        if not 0 < delivery_timeout_seconds <= 60:
            raise ValueError("delivery_timeout_seconds must be between zero and 60")
        if not 0 < consumer_timeout_seconds <= 10:
            raise ValueError("consumer_timeout_seconds must be between zero and 10")
        if not 512 <= max_datagram_bytes <= 65_535:
            raise ValueError("max_datagram_bytes must be between 512 and 65535")
        if not 8_192 <= receive_buffer_bytes <= 1_048_576:
            raise ValueError("receive_buffer_bytes must be between 8192 and 1048576")
        self._packet_goal = packet_goal
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._consumer_timeout_seconds = consumer_timeout_seconds
        self._max_datagram_bytes = max_datagram_bytes
        self._receive_buffer_bytes = receive_buffer_bytes
        self._relay_startup_probe_seconds = relay_startup_probe_seconds
        self._relay_factory = relay_factory

    def _make_relay(self, port: int) -> RtpRelay:
        if self._relay_factory is not None:
            return self._relay_factory(port)
        return GStreamerRtpRelayRuntime(
            port,
            startup_probe_seconds=self._relay_startup_probe_seconds,
        )

    async def deliver(self, source_uri: str, consumer: RtpConsumer) -> RtpDeliveryResult:
        """Deliver a bounded packet sample; payload bytes are never retained."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._receive_buffer_bytes)
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        relay = self._make_relay(port)
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        valid_packets = 0
        invalid_packets = 0
        delivered_bytes = 0

        try:
            try:
                await relay.start(source_uri)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise RtpDeliveryError(
                    RtpDeliveryErrorCode.RUNTIME_FAILURE,
                    "RTP relay failed to start",
                ) from None

            deadline = started + self._delivery_timeout_seconds
            while valid_packets < self._packet_goal:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RtpDeliveryError(
                        RtpDeliveryErrorCode.TIMEOUT,
                        "RTP delivery timed out",
                    )
                try:
                    packet, _ = await asyncio.wait_for(
                        loop.sock_recvfrom(sock, self._max_datagram_bytes),
                        timeout=remaining,
                    )
                except TimeoutError:
                    raise RtpDeliveryError(
                        RtpDeliveryErrorCode.TIMEOUT,
                        "RTP delivery timed out",
                    ) from None

                if not is_rtp_v2(packet):
                    invalid_packets += 1
                    continue

                try:
                    await asyncio.wait_for(
                        consumer(memoryview(packet)),
                        timeout=self._consumer_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise RtpDeliveryError(
                        RtpDeliveryErrorCode.CONSUMER_FAILURE,
                        "RTP consumer failed",
                    ) from None

                valid_packets += 1
                delivered_bytes += len(packet)

            return RtpDeliveryResult(
                valid_packets=valid_packets,
                invalid_packets=invalid_packets,
                delivered_bytes=delivered_bytes,
                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        finally:
            try:
                await relay.close()
            finally:
                sock.close()
