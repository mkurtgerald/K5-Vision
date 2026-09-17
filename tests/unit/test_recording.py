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
        self.fail_open = False
        self.fail_write = False
        self.fail_finalize = False
        self.write_delay_seconds = 0.0
        self.write_started = asyncio.Event()

    async def open(self) -> None:
        self.opens += 1
        if self.fail_open:
            raise RuntimeError("unsafe open detail: storage-secret")

    async def write(self, packet: memoryview) -> None:
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        self.write_started.set()
        try:
            if self.write_delay_seconds:
                await asyncio.sleep(self.write_delay_seconds)
            else:
                await asyncio.sleep(0)
            if self.fail_write:
                raise RuntimeError(f"unsafe sink detail {bytes(packet)!r}")
            self.writes += 1
        finally:
            self.active_writes -= 1

    async def finalize(self) -> None:
        self.finalizes += 1
        if self.fail_finalize:
            raise RuntimeError("unsafe finalize detail: storage-secret")

    async def abort(self) -> None:
        self.aborts += 1


def test_recording_lifecycle_is_bounded_and_finalize_is_idempotent() -> None:
    async def exercise() -> None:
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

    asyncio.run(exercise())


def test_invalid_rtp_is_rejected_before_sink_write() -> None:
    async def exercise() -> None:
        sink = FakeSink()
        recorder = BoundedRtpRecorder(sink)
        await recorder.start()

        with pytest.raises(RecordingError) as caught:
            await recorder.consume(memoryview(b"not-rtp"))

        assert caught.value.code == RecordingErrorCode.INVALID_PACKET
        assert sink.writes == 0
        assert recorder.snapshot.state == RecordingState.RECORDING

    asyncio.run(exercise())


def test_packet_limit_aborts_without_undefined_state() -> None:
    async def exercise() -> None:
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

    asyncio.run(exercise())


def test_byte_limit_aborts_before_second_sink_write() -> None:
    async def exercise() -> None:
        sink = FakeSink()
        packet = rtp_packet(b"bounded")
        recorder = BoundedRtpRecorder(sink, max_bytes=len(packet), max_packets=8)
        await recorder.start()
        await recorder.consume(memoryview(packet))

        with pytest.raises(RecordingError) as caught:
            await recorder.consume(memoryview(packet))

        assert caught.value.code == RecordingErrorCode.LIMIT_EXCEEDED
        assert recorder.snapshot.state == RecordingState.ABORTED
        assert recorder.snapshot.bytes_written == len(packet)
        assert sink.writes == 1
        assert sink.aborts == 1

    asyncio.run(exercise())


def test_sink_failure_is_sanitized_and_aborted() -> None:
    async def exercise() -> None:
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

    asyncio.run(exercise())


def test_open_failure_is_sanitized_and_cleanup_runs() -> None:
    async def exercise() -> None:
        sink = FakeSink()
        sink.fail_open = True
        recorder = BoundedRtpRecorder(sink)

        with pytest.raises(RecordingError) as caught:
            await recorder.start()

        assert caught.value.code == RecordingErrorCode.SINK_FAILURE
        assert "storage-secret" not in str(caught.value)
        assert recorder.snapshot.state == RecordingState.FAILED
        assert sink.aborts == 1

    asyncio.run(exercise())


def test_write_timeout_is_bounded_and_cleanup_runs() -> None:
    async def exercise() -> None:
        sink = FakeSink()
        sink.write_delay_seconds = 0.1
        recorder = BoundedRtpRecorder(sink, operation_timeout_seconds=0.01)
        await recorder.start()

        with pytest.raises(RecordingError) as caught:
            await recorder.consume(memoryview(rtp_packet()))

        assert caught.value.code == RecordingErrorCode.TIMEOUT
        assert recorder.snapshot.state == RecordingState.FAILED
        assert recorder.snapshot.packets_written == 0
        assert sink.aborts == 1

    asyncio.run(exercise())


def test_finalize_failure_is_sanitized_and_cleanup_runs() -> None:
    async def exercise() -> None:
        sink = FakeSink()
        sink.fail_finalize = True
        recorder = BoundedRtpRecorder(sink)
        await recorder.start()
        await recorder.consume(memoryview(rtp_packet()))

        with pytest.raises(RecordingError) as caught:
            await recorder.finalize()

        assert caught.value.code == RecordingErrorCode.SINK_FAILURE
        assert "storage-secret" not in str(caught.value)
        assert recorder.snapshot.state == RecordingState.FAILED
        assert recorder.snapshot.packets_written == 1
        assert sink.aborts == 1

    asyncio.run(exercise())


def test_cancelled_write_aborts_recorder_and_sink() -> None:
    async def exercise() -> None:
        sink = FakeSink()
        sink.write_delay_seconds = 10.0
        recorder = BoundedRtpRecorder(sink, operation_timeout_seconds=30.0)
        await recorder.start()

        task = asyncio.create_task(recorder.consume(memoryview(rtp_packet())))
        await sink.write_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert recorder.snapshot.state == RecordingState.ABORTED
        assert recorder.snapshot.packets_written == 0
        assert sink.aborts == 1

    asyncio.run(exercise())


def test_concurrent_writes_are_serialized() -> None:
    async def exercise() -> None:
        sink = FakeSink()
        recorder = BoundedRtpRecorder(sink, max_packets=8)
        await recorder.start()

        await asyncio.gather(
            *(recorder.consume(memoryview(rtp_packet(bytes([index])))) for index in range(8))
        )

        assert recorder.snapshot.packets_written == 8
        assert sink.writes == 8
        assert sink.max_active_writes == 1

    asyncio.run(exercise())


def test_abort_is_idempotent_and_finalize_after_abort_is_rejected() -> None:
    async def exercise() -> None:
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

    asyncio.run(exercise())


def test_finalized_recording_rejects_abort_and_reentry() -> None:
    async def exercise() -> None:
        sink = FakeSink()
        recorder = BoundedRtpRecorder(sink)
        await recorder.start()
        await recorder.finalize()

        with pytest.raises(RecordingError) as abort_error:
            await recorder.abort()
        assert abort_error.value.code == RecordingErrorCode.INVALID_STATE

        with pytest.raises(RecordingError) as reentry_error:
            await recorder.start()
        assert reentry_error.value.code == RecordingErrorCode.INVALID_STATE

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_packets": 0}, "max_packets"),
        ({"max_packets": 1_000_001}, "max_packets"),
        ({"max_bytes": 0}, "max_bytes"),
        ({"max_bytes": 8 * 1024 * 1024 * 1024 + 1}, "max_bytes"),
        ({"operation_timeout_seconds": 0}, "operation_timeout_seconds"),
        ({"operation_timeout_seconds": 30.1}, "operation_timeout_seconds"),
    ],
)
def test_invalid_recorder_bounds_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        BoundedRtpRecorder(FakeSink(), **kwargs)
