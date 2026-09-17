from __future__ import annotations

import asyncio

import pytest

from k5vision.media.session import (
    MediaSession,
    MediaSessionError,
    MediaSessionErrorCode,
    MediaSessionState,
)


class FakeRuntime:
    def __init__(
        self,
        *,
        fail_start: bool = False,
        fail_stop: bool = False,
        fail_close: bool = False,
        cancel_start: bool = False,
    ) -> None:
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.fail_close = fail_close
        self.cancel_start = cancel_start
        self.starts = 0
        self.stops = 0
        self.closes = 0

    async def start(self, source_uri: str) -> None:
        self.starts += 1
        if self.cancel_start:
            raise asyncio.CancelledError
        if self.fail_start:
            raise RuntimeError(f"unsafe runtime detail: {source_uri}")

    async def stop(self) -> None:
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("unsafe stop detail")

    async def close(self) -> None:
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("unsafe close detail")


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_session_lifecycle_is_deterministic_and_idempotent() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        session = MediaSession(runtime)
        assert session.snapshot.schema_version == "1"
        assert session.snapshot.state == MediaSessionState.CREATED

        first = await session.start("rtsp://secret@example.invalid/stream")
        second = await session.start("rtsp://secret@example.invalid/stream")
        assert first.state == second.state == MediaSessionState.RUNNING
        assert first.generation == second.generation == 1
        assert runtime.starts == 1

        await session.stop()
        await session.stop()
        assert session.snapshot.state == MediaSessionState.STOPPED
        assert runtime.stops == 1

        await session.start("rtsp://secret@example.invalid/stream")
        assert session.snapshot.generation == 2
        await session.close()
        await session.close()
        assert session.snapshot.state == MediaSessionState.CLOSED
        assert runtime.stops == 2
        assert runtime.closes == 1

    run(scenario())


def test_runtime_start_failure_is_sanitized_recoverable_and_restartable() -> None:
    async def scenario() -> None:
        source = "rtsp://username:password@192.0.2.1/stream"
        runtime = FakeRuntime(fail_start=True)
        session = MediaSession(runtime)
        with pytest.raises(MediaSessionError) as caught:
            await session.start(source)
        assert caught.value.code == MediaSessionErrorCode.RUNTIME_FAILURE
        assert session.snapshot.state == MediaSessionState.FAILED
        assert source not in str(caught.value)
        assert "username" not in str(caught.value)
        assert "password" not in str(caught.value)

        with pytest.raises(MediaSessionError) as restart:
            await session.start(source)
        assert restart.value.code == MediaSessionErrorCode.INVALID_STATE

        with pytest.raises(MediaSessionError) as close:
            await session.close()
        assert close.value.code == MediaSessionErrorCode.INVALID_STATE

        runtime.fail_start = False
        recovered = await session.recover()
        assert recovered.state == MediaSessionState.STOPPED
        assert runtime.closes == 1
        restarted = await session.start(source)
        assert restarted.state == MediaSessionState.RUNNING
        assert restarted.generation == 1

    run(scenario())


def test_recovery_requires_failed_state_and_sanitizes_cleanup_failure() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(fail_start=True, fail_close=True)
        session = MediaSession(runtime)
        with pytest.raises(MediaSessionError):
            await session.recover()
        with pytest.raises(MediaSessionError):
            await session.start("rtsp://example.invalid/stream")
        with pytest.raises(MediaSessionError) as caught:
            await session.recover()
        assert caught.value.code == MediaSessionErrorCode.RUNTIME_FAILURE
        assert "unsafe close detail" not in str(caught.value)
        assert session.snapshot.state == MediaSessionState.FAILED

    run(scenario())


def test_stop_failure_is_sanitized() -> None:
    async def scenario() -> None:
        session = MediaSession(FakeRuntime(fail_stop=True))
        await session.start("rtsp://example.invalid/stream")
        with pytest.raises(MediaSessionError) as caught:
            await session.stop()
        assert caught.value.code == MediaSessionErrorCode.RUNTIME_FAILURE
        assert "unsafe stop detail" not in str(caught.value)
        assert session.snapshot.state == MediaSessionState.FAILED

    run(scenario())


def test_close_failure_is_sanitized() -> None:
    async def scenario() -> None:
        session = MediaSession(FakeRuntime(fail_close=True))
        with pytest.raises(MediaSessionError) as caught:
            await session.close()
        assert caught.value.code == MediaSessionErrorCode.RUNTIME_FAILURE
        assert "unsafe close detail" not in str(caught.value)
        assert session.snapshot.state == MediaSessionState.FAILED

    run(scenario())


def test_empty_source_rejected_before_runtime() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        session = MediaSession(runtime)
        with pytest.raises(MediaSessionError) as caught:
            await session.start("   ")
        assert caught.value.code == MediaSessionErrorCode.INVALID_SOURCE
        assert runtime.starts == 0

    run(scenario())


def test_closed_session_rejects_restart_and_stop() -> None:
    async def scenario() -> None:
        session = MediaSession(FakeRuntime())
        await session.close()
        with pytest.raises(MediaSessionError) as restart:
            await session.start("rtsp://example.invalid/stream")
        assert restart.value.code == MediaSessionErrorCode.INVALID_STATE
        with pytest.raises(MediaSessionError) as stop:
            await session.stop()
        assert stop.value.code == MediaSessionErrorCode.INVALID_STATE

    run(scenario())


def test_concurrent_start_is_serialized_to_one_runtime_start() -> None:
    async def scenario() -> None:
        class BlockingRuntime(FakeRuntime):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def start(self, source_uri: str) -> None:
                self.starts += 1
                self.entered.set()
                await self.release.wait()

        runtime = BlockingRuntime()
        session = MediaSession(runtime)
        first = asyncio.create_task(session.start("rtsp://example.invalid/stream"))
        await runtime.entered.wait()
        second = asyncio.create_task(session.start("rtsp://example.invalid/stream"))
        await asyncio.sleep(0)
        assert runtime.starts == 1
        runtime.release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.state == second_result.state == MediaSessionState.RUNNING
        assert runtime.starts == 1

    run(scenario())


def test_cancelled_start_transitions_to_failed_for_explicit_recovery() -> None:
    async def scenario() -> None:
        session = MediaSession(FakeRuntime(cancel_start=True))
        with pytest.raises(asyncio.CancelledError):
            await session.start("rtsp://example.invalid/stream")
        assert session.snapshot.state == MediaSessionState.FAILED

    run(scenario())
