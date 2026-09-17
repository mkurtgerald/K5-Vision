from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from k5vision.media.live_view import (
    LiveViewBoundary,
    LiveViewError,
    LiveViewErrorCode,
    LiveViewState,
)
from k5vision.media.session import MediaSession


class FakeRuntime:
    def __init__(self, *, start_delay: float = 0.0, fail_start: bool = False) -> None:
        self.start_delay = start_delay
        self.fail_start = fail_start
        self.starts = 0
        self.stops = 0
        self.closes = 0

    async def start(self, source_uri: str) -> None:
        self.starts += 1
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if self.fail_start:
            raise RuntimeError(f"unsafe source detail: {source_uri}")

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closes += 1


@pytest.mark.asyncio
async def test_concurrent_consumers_share_one_runtime_start() -> None:
    runtime = FakeRuntime()
    boundary = LiveViewBoundary(MediaSession(runtime), max_consumers=4)
    source = "rtsp://user:secret@192.0.2.20/live"

    first, second = await asyncio.gather(
        boundary.acquire(source),
        boundary.acquire(source),
    )

    assert first.lease_id != second.lease_id
    assert runtime.starts == 1
    assert boundary.snapshot.active_consumers == 2
    assert boundary.snapshot.state == LiveViewState.RUNNING

    await boundary.release(first.lease_id)
    assert runtime.stops == 0
    assert boundary.snapshot.active_consumers == 1

    await boundary.release(second.lease_id)
    assert runtime.stops == 1
    assert boundary.snapshot.active_consumers == 0
    assert boundary.snapshot.state == LiveViewState.IDLE


@pytest.mark.asyncio
async def test_reentry_starts_new_generation_after_final_release() -> None:
    runtime = FakeRuntime()
    boundary = LiveViewBoundary(MediaSession(runtime))

    first = await boundary.acquire("rtsp://example.invalid/live")
    await boundary.release(first.lease_id)
    second = await boundary.acquire("rtsp://example.invalid/live")

    assert first.generation == 1
    assert second.generation == 2
    assert runtime.starts == 2


@pytest.mark.asyncio
async def test_capacity_is_bounded_without_additional_runtime_start() -> None:
    runtime = FakeRuntime()
    boundary = LiveViewBoundary(MediaSession(runtime), max_consumers=1)
    first = await boundary.acquire("rtsp://example.invalid/live")

    with pytest.raises(LiveViewError) as caught:
        await boundary.acquire("rtsp://example.invalid/live")

    assert caught.value.code == LiveViewErrorCode.CAPACITY
    assert runtime.starts == 1
    await boundary.release(first.lease_id)


@pytest.mark.asyncio
async def test_invalid_lease_is_sanitized_and_does_not_stop_active_session() -> None:
    runtime = FakeRuntime()
    boundary = LiveViewBoundary(MediaSession(runtime))
    lease = await boundary.acquire("rtsp://example.invalid/live")

    with pytest.raises(LiveViewError) as caught:
        await boundary.release(uuid4())

    assert caught.value.code == LiveViewErrorCode.INVALID_LEASE
    assert runtime.stops == 0
    await boundary.release(lease.lease_id)


@pytest.mark.asyncio
async def test_runtime_failure_does_not_leak_source_or_create_lease() -> None:
    source = "rtsp://username:password@192.0.2.44/private"
    boundary = LiveViewBoundary(MediaSession(FakeRuntime(fail_start=True)))

    with pytest.raises(LiveViewError) as caught:
        await boundary.acquire(source)

    rendered = str(caught.value)
    assert caught.value.code == LiveViewErrorCode.SESSION_FAILURE
    assert source not in rendered
    assert "username" not in rendered
    assert "password" not in rendered
    assert "192.0.2.44" not in rendered
    assert boundary.snapshot.active_consumers == 0
    assert boundary.snapshot.state == LiveViewState.FAILED


@pytest.mark.asyncio
async def test_start_timeout_recovers_then_allows_clean_reentry() -> None:
    source = "rtsp://username:password@192.0.2.55/private"
    runtime = FakeRuntime(start_delay=0.1)
    boundary = LiveViewBoundary(
        MediaSession(runtime),
        operation_timeout_seconds=0.01,
    )

    with pytest.raises(LiveViewError) as caught:
        await boundary.acquire(source)

    assert caught.value.code == LiveViewErrorCode.OPERATION_TIMEOUT
    assert source not in str(caught.value)
    assert boundary.snapshot.active_consumers == 0
    assert boundary.snapshot.state == LiveViewState.FAILED

    runtime.start_delay = 0.0
    recovered = await boundary.recover()
    assert recovered.state == LiveViewState.IDLE
    assert runtime.closes == 1

    lease = await boundary.acquire(source)
    assert lease.generation == 1
    assert boundary.snapshot.state == LiveViewState.RUNNING
    await boundary.release(lease.lease_id)


@pytest.mark.asyncio
async def test_failed_boundary_requires_recovery_before_new_acquire() -> None:
    runtime = FakeRuntime(fail_start=True)
    boundary = LiveViewBoundary(MediaSession(runtime))

    with pytest.raises(LiveViewError):
        await boundary.acquire("rtsp://example.invalid/live")

    runtime.fail_start = False
    with pytest.raises(LiveViewError) as caught:
        await boundary.acquire("rtsp://example.invalid/live")

    assert caught.value.code == LiveViewErrorCode.INVALID_STATE
    assert runtime.starts == 1

    await boundary.recover()
    lease = await boundary.acquire("rtsp://example.invalid/live")
    assert lease.generation == 1


@pytest.mark.asyncio
async def test_close_recovers_failed_session_before_final_cleanup() -> None:
    runtime = FakeRuntime(fail_start=True)
    boundary = LiveViewBoundary(MediaSession(runtime))

    with pytest.raises(LiveViewError):
        await boundary.acquire("rtsp://example.invalid/live")

    snapshot = await boundary.close()

    assert snapshot.state == LiveViewState.CLOSED
    assert snapshot.active_consumers == 0
    assert runtime.closes == 2


@pytest.mark.asyncio
async def test_close_clears_consumers_stops_and_closes_session() -> None:
    runtime = FakeRuntime()
    boundary = LiveViewBoundary(MediaSession(runtime), max_consumers=2)
    await boundary.acquire("rtsp://example.invalid/live")
    await boundary.acquire("rtsp://example.invalid/live")

    snapshot = await boundary.close()

    assert snapshot.state == LiveViewState.CLOSED
    assert snapshot.active_consumers == 0
    assert runtime.starts == 1
    assert runtime.stops == 1
    assert runtime.closes == 1
