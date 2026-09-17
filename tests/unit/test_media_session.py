from __future__ import annotations

import pytest

from k5vision.media.session import MediaSession, MediaSessionError, MediaSessionState


class FakeRuntime:
    def __init__(self, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.starts = 0
        self.stops = 0
        self.closes = 0

    async def start(self, source_uri: str) -> None:
        self.starts += 1
        if self.fail_start:
            raise RuntimeError(f"unsafe runtime detail: {source_uri}")

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closes += 1


@pytest.mark.asyncio
async def test_session_lifecycle_is_deterministic_and_idempotent() -> None:
    runtime = FakeRuntime()
    session = MediaSession(runtime)
    assert session.snapshot.state == MediaSessionState.CREATED

    first = await session.start("rtsp://secret@example.invalid/stream")
    second = await session.start("rtsp://secret@example.invalid/stream")
    assert first.state == second.state == MediaSessionState.RUNNING
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
    assert runtime.closes == 1


@pytest.mark.asyncio
async def test_runtime_failure_is_sanitized() -> None:
    source = "rtsp://username:password@192.0.2.1/stream"
    session = MediaSession(FakeRuntime(fail_start=True))
    with pytest.raises(MediaSessionError) as caught:
        await session.start(source)
    assert session.snapshot.state == MediaSessionState.FAILED
    assert source not in str(caught.value)
    assert "username" not in str(caught.value)
    assert "password" not in str(caught.value)


@pytest.mark.asyncio
async def test_empty_source_rejected_before_runtime() -> None:
    runtime = FakeRuntime()
    session = MediaSession(runtime)
    with pytest.raises(MediaSessionError):
        await session.start("   ")
    assert runtime.starts == 0
