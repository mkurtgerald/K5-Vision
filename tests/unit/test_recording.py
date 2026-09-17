from __future__ import annotations

import asyncio

import pytest

from k5vision.media.recording import (
    BoundedRtpRecorder,
    RecordingError,
    RecordingErrorCode,
    RecordingState,
)


def rtp_packet(payload: bytes = b"payload") -> bytes:
    return bytes([0x80, 0x60, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]) + payload


class FakeSink:
    def __init__(self) -> None:
        self.opens = 0
        self.writes = 0
        self.finalizes = 0
        self.aborts = 0
        self.active_writes = 0
        self.max_active_writes = 0
        self.fail_write = False

    async def open(self) -> None:
        self.opens += 1

    async def write(self, packet: memoryview) -> None:
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            await asyncio.sleep(0)
            if self.fail_write:
                raise RuntimeError(f"unsafe sink detail {bytes(packet)!r}")
            self.writes += 1
        finally:
            self.active_writes -= 1

    async def finalize(self) -> None:
        self.finalizes += 1

    async def abort(self) -> None:
        self.aborts += 1


@pytest.mark.asyncio
async def test_recording_lifecycle_is_bounded_and_finalize_is_idempotent() -> None:
    sink = FakeSink()
    recorder = BoundedRtpRecorder(sink, max_packets=2, max_bytes=4096)

    await recorder.start()
    await recorder.start()
    await recorder.consume(memoryview(rtp_packet(b"one")))
    await recorder.consume(memoryview(rtp_packet(b"two")))
    first = await recorder.finalize()
    second = await recorder.finalize()

    assert first == second
    assert first.state == RecordingState.FINALIZED
    assert first.packets_written == 2
    assert sink.opens == 1
    assert sink.writes == 2
    assert sink.finalizes == 1
    assert "payload" not in first.model_dump_json()


@pytest.mark.asyncio
async def test_invalid_rtp_is_rejected_before_sink_write() -> None:
    sink = FakeSink()
    recorder = BoundedRtpRecorder(sink)
    await recorder.start()

    with pytest.raises(RecordingError) as caught:
        await recorder.consume(memoryview(b"not-rtp"))

    assert caught.value.code == RecordingErrorCode.INVALID_PACKET
    assert sink.writes == 0
    assert recorder.snapshot.state == RecordingState.RECORDING


@pytest.mark.asyncio
async def test_packet_limit_aborts_without_undefined_state() -> None:
    sink = FakeSink()
    recorder = BoundedRtpRecorder(sink, max_packets=1)
    await recorder.start()
    await recorder.consume(memoryview(rtp_packet()))

    with pytest.raises(RecordingError) as caught:
        await recorder.consume(memoryview(rtp_packet()))

    assert caught.value.code == RecordingErrorCode.LIMIT_EXCEEDED
    assert recorder.snapshot.state == RecordingState.ABORTED
    assert recorder.snapshot.packets_written == 1
    assert sink.aborts == 1


@pytest.mark.asyncio
async def test_sink_failure_is_sanitized_and_aborted() -> None:
    sink = FakeSink()
    sink.fail_write = True
    recorder = BoundedRtpRecorder(sink)
    packet = rtp_packet(b"super-secret-payload")
    await recorder.start()

    with pytest.raises(RecordingError) as caught:
        await recorder.consume(memoryview(packet))

    assert caught.value.code == RecordingErrorCode.SINK_FAILURE
    assert "super-secret" not in str(caught.value)
    assert recorder.snapshot.state == RecordingState.FAILED
    assert recorder.snapshot.packets_written == 0
    assert sink.aborts == 1


@pytest.mark.asyncio
async def test_concurrent_writes_are_serialized() -> None:
    sink = FakeSink()
    recorder = BoundedRtpRecorder(sink, max_packets=8)
    await recorder.start()

    await asyncio.gather(
        *(recorder.consume(memoryview(rtp_packet(bytes([index])))) for index in range(8))
    )

    assert recorder.snapshot.packets_written == 8
    assert sink.writes == 8
    assert sink.max_active_writes == 1


@pytest.mark.asyncio
async def test_abort_is_idempotent_and_finalize_after_abort_is_rejected() -> None:
    sink = FakeSink()
    recorder = BoundedRtpRecorder(sink)
    await recorder.start()
    await recorder.abort()
    await recorder.abort()

    assert recorder.snapshot.state == RecordingState.ABORTED
    assert sink.aborts == 1
    with pytest.raises(RecordingError) as caught:
        await recorder.finalize()
    assert caught.value.code == RecordingErrorCode.INVALID_STATE
